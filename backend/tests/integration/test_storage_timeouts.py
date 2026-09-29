"""``shared.storage`` y ``ledger.evidence`` contra LocalStack (TASK-112; LC-NUC-15, LC-NUC-30).

- PR-NUC-22 con sumas de verificación reales: el almacén calcula ``x-amz-checksum-sha256`` sobre
  los bytes recibidos y cada combinación de fallos produce su código (BR-NUC-64).
- Dos evidencias de un registro se verifican en paralelo: con 300 ms de latencia inyectada por
  un intermediario TCP, dos consultas duran lo que una (NFR-NUC-01, PAT-NUC-REN-06).
- Con LocalStack detenido o congelado, la verificación termina en su tiempo de espera con
  ``StorageUnavailable`` (transitorio, ``storage_unavailable`` hacia el nodo; NFR-NUC-36, 37).
- ``presign_put``: un ``PUT`` sin la cabecera de suma, o con otra suma, lo rechaza el almacén;
  la subida por partes completa deja un objeto cuya suma devuelve ``head_object``.

LocalStack valida las firmas (``S3_SKIP_SIGNATURE_VALIDATION=0`` en ``conftest.py`` y en
``docker-compose.yml``), como S3. Solo bytes generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import enum
import hashlib
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from vigia_contracts.clock import SimulatedClock, SystemClock
from vigia_contracts.models.clip_reference import ClipReference

from tests.integration.conftest import (
    LOCALSTACK_ENVIRONMENT,
    LOCALSTACK_IMAGE,
    LOCALSTACK_PORT,
    STARTUP_TIMEOUT_SECONDS,
    LocalStackEndpoint,
    _start,
    _wait_for_localstack,
    versioned_bucket,
)
from tests.properties.evidence_strategies import VIDEO, clip_reference
from vigia_platform.ledger.evidence import (
    EvidenceFailure,
    EvidenceOwner,
    EvidenceVerifier,
    first_failure,
)
from vigia_platform.shared.storage import (
    ANONYMIZED_HEADER,
    CHECKSUM_HEADER,
    ChecksumType,
    CompletedPart,
    S3Storage,
    StorageSettings,
    StorageUnavailable,
    sha256_b64,
)

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 29, 12, tzinfo=UTC)
WALL_CLOCK = SystemClock()
"""Reloj real para medir duraciones: estas pruebas miden tiempo de verdad a propósito."""
HTTP_TIMEOUT_SECONDS = 30.0
OWNER = EvidenceOwner(
    uuid.UUID("5d0c1c4e-3b7a-4f0e-9a51-7c2f1b9e0a11"),
    uuid.UUID("5d0c1c4e-3b7a-4f0e-9a51-7c2f1b9e0a12"),
    uuid.UUID("5d0c1c4e-3b7a-4f0e-9a51-7c2f1b9e0a13"),
    uuid.UUID("5d0c1c4e-3b7a-4f0e-9a51-7c2f1b9e0a14"),
)
FOREIGN_NODE = uuid.UUID("5d0c1c4e-3b7a-4f0e-9a51-7c2f1b9e0a99")


def _clip_id(n: int) -> uuid.UUID:
    return uuid.UUID(f"01920000-0000-7000-8000-{n:012x}")


def _body(n: int, size: int = 4096) -> bytes:
    seed = hashlib.sha256(f"vigia-task-112-clip-{n}".encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def _verifier(storage: S3Storage) -> EvidenceVerifier:
    return EvidenceVerifier(storage, SimulatedClock(START))


def _verify(storage: S3Storage, refs: list[ClipReference]) -> list[Any]:
    return asyncio.run(_verifier(storage).verify_references(OWNER, refs))


@pytest.fixture(scope="module")
def s3(localstack_endpoint: LocalStackEndpoint) -> Any:
    return localstack_endpoint.aws_client("s3")


@pytest.fixture(scope="module")
def bucket(s3: Any) -> Iterator[str]:
    with versioned_bucket(s3, "vigia-evidence-t112") as name:
        yield name


@pytest.fixture(scope="module")
def storage(localstack_endpoint: LocalStackEndpoint, bucket: str) -> S3Storage:
    return S3Storage(localstack_endpoint.storage_settings(bucket), SimulatedClock(START))


# --- PR-NUC-22 con LocalStack ------------------------------------------------------------------


class Stored(enum.StrEnum):
    """Cómo quedó el objeto en el almacén."""

    OK = "ok"
    ABSENT = "absent"
    NO_MARKER = "no_marker"
    MARKER_ZERO = "marker_zero"
    NO_SHA256 = "no_sha256"


class Tamper(enum.StrEnum):
    """Qué cambia la referencia del nodo frente al objeto."""

    NONE = "none"
    SIZE = "size"
    SHA = "sha"
    FOREIGN_KEY = "foreign_key"


@dataclass(frozen=True)
class StoredClip:
    clip_id: uuid.UUID
    body: bytes

    def reference(self, tamper: Tamper) -> ClipReference:
        sha = hashlib.sha256(self.body).hexdigest()
        size = len(self.body)
        if tamper is Tamper.SIZE:
            size += 1
        if tamper is Tamper.SHA:
            sha = hashlib.sha256(self.body + b"x").hexdigest()
        reference = clip_reference(
            OWNER, clip_id=self.clip_id, content_type=VIDEO, sha256=sha, size_bytes=size
        )
        if tamper is Tamper.FOREIGN_KEY:
            key = reference.storage_key.replace(str(OWNER.node_id), str(FOREIGN_NODE))
            reference = reference.model_copy(update={"storage_key": key})
        return reference


def _oracle(stored: Stored, tamper: Tamper) -> EvidenceFailure | None:
    if tamper is Tamper.FOREIGN_KEY:
        return EvidenceFailure.STORAGE_KEY_MISMATCH
    if stored is Stored.ABSENT:
        return EvidenceFailure.OBJECT_ABSENT
    if tamper is Tamper.SIZE:
        return EvidenceFailure.SIZE_MISMATCH
    if stored is Stored.NO_SHA256:
        return EvidenceFailure.CHECKSUM_ABSENT
    if tamper is Tamper.SHA:
        return EvidenceFailure.CHECKSUM_MISMATCH
    if stored is Stored.NO_MARKER:
        return EvidenceFailure.MARKER_ABSENT
    if stored is Stored.MARKER_ZERO:
        return EvidenceFailure.MARKER_INVALID
    return None


@pytest.fixture(scope="module")
def stored_clips(s3: Any, bucket: str) -> dict[Stored, StoredClip]:
    """Un objeto por variante, subido como lo subiría el nodo (o sin lo que falta)."""
    clips: dict[Stored, StoredClip] = {}
    for n, variant in enumerate(Stored, start=1):
        clip = StoredClip(_clip_id(n), _body(n))
        clips[variant] = clip
        key = clip.reference(Tamper.NONE).storage_key
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": clip.body,
            "ContentType": VIDEO,
            "ChecksumSHA256": sha256_b64(clip.body),
            "Metadata": {"vigia-anonymized": "1"},
        }
        if variant is Stored.ABSENT:
            continue
        if variant is Stored.NO_MARKER:
            params["Metadata"] = {}
        if variant is Stored.MARKER_ZERO:
            params["Metadata"] = {"vigia-anonymized": "0"}
        if variant is Stored.NO_SHA256:
            del params["ChecksumSHA256"]
            params["ChecksumAlgorithm"] = "CRC32"
        s3.put_object(**params)
    return clips


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(
        st.tuples(st.sampled_from(list(Stored)), st.sampled_from(list(Tamper))),
        min_size=1,
        max_size=3,
    )
)
def test_pr_nuc_22_against_localstack(
    storage: S3Storage,
    stored_clips: dict[Stored, StoredClip],
    record: list[tuple[Stored, Tamper]],
) -> None:
    refs = [stored_clips[stored].reference(tamper) for stored, tamper in record]
    checks = _verify(storage, refs)
    expected = [_oracle(stored, tamper) for stored, tamper in record]
    assert [check.failure for check in checks] == expected
    assert (first_failure(checks) is None) is all(failure is None for failure in expected)


def test_ok_object_exposes_checksum_size_and_marker(
    storage: S3Storage, stored_clips: dict[Stored, StoredClip]
) -> None:
    clip = stored_clips[Stored.OK]
    head = asyncio.run(storage.head_object(clip.reference(Tamper.NONE).storage_key))
    assert head is not None
    assert head.checksum_sha256 == sha256_b64(clip.body)
    assert head.checksum_type is ChecksumType.FULL_OBJECT
    assert head.full_object_sha256_hex == hashlib.sha256(clip.body).hexdigest()
    assert head.size_bytes == len(clip.body)
    assert head.metadata == {"vigia-anonymized": "1"}


# --- Paralelismo medido ------------------------------------------------------------------------


class LatencyProxy:
    """Intermediario TCP que retrasa cada petición ``delay`` segundos antes de reenviarla."""

    def __init__(self, upstream: tuple[str, int], delay: float) -> None:
        self._upstream = upstream
        self._delay = delay
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.getsockname()[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> LatencyProxy:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._server.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            upstream = socket.create_connection(self._upstream, timeout=30)
            threading.Thread(target=self._pipe, args=(client, upstream, True), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client, False), daemon=True).start()

    def _pipe(self, source: socket.socket, target: socket.socket, delayed: bool) -> None:
        with contextlib.suppress(OSError):
            while chunk := source.recv(65536):
                if delayed:
                    time.sleep(self._delay)
                target.sendall(chunk)
        with contextlib.suppress(OSError):
            target.shutdown(socket.SHUT_WR)


def _elapsed(storage: S3Storage, refs: list[ClipReference]) -> float:
    started = WALL_CLOCK.monotonic()
    checks = _verify(storage, refs)
    elapsed = WALL_CLOCK.monotonic() - started
    assert all(check.ok for check in checks)
    return elapsed


def test_two_evidences_of_a_record_are_verified_in_parallel(
    localstack_endpoint: LocalStackEndpoint,
    bucket: str,
    s3: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delay = 0.3
    clips = [StoredClip(_clip_id(100 + n), _body(100 + n)) for n in range(2)]
    refs = [clip.reference(Tamper.NONE) for clip in clips]
    for clip, ref in zip(clips, refs, strict=True):
        s3.put_object(
            Bucket=bucket,
            Key=ref.storage_key,
            Body=clip.body,
            ChecksumSHA256=sha256_b64(clip.body),
            Metadata={"vigia-anonymized": "1"},
        )
    host_port = localstack_endpoint.url.removeprefix("http://").rsplit(":", 1)
    with LatencyProxy((host_port[0], int(host_port[1])), delay) as proxy:
        settings_ = localstack_endpoint.storage_settings(bucket)
        slow = S3Storage(
            StorageSettings(
                bucket=bucket,
                endpoint_url=proxy.url,
                addressing_style=settings_.addressing_style,
                credentials=settings_.credentials,
            ),
            SimulatedClock(START),
        )
        _elapsed(slow, refs[:1])  # calienta el cliente
        one = min(_elapsed(slow, refs[:1]) for _ in range(3))
        two = min(_elapsed(slow, refs) for _ in range(3))
    with capsys.disabled():
        print(
            f"\nverificación con {delay * 1000:.0f} ms de latencia: 1 evidencia {one:.3f} s, "
            f"2 evidencias {two:.3f} s (en serie serían ≈ {2 * one:.3f} s)"
        )
    assert one >= delay
    assert two < 1.5 * one, (one, two)


# --- Subida prefirmada -------------------------------------------------------------------------


def _key(n: int) -> str:
    return clip_reference(
        OWNER, clip_id=_clip_id(n), content_type=VIDEO, sha256="0" * 64, size_bytes=1
    ).storage_key


def test_presign_put_accepts_exact_headers_and_object_verifies(storage: S3Storage) -> None:
    body = _body(200)
    key = _key(200)
    grant = asyncio.run(
        storage.presign_put(key, VIDEO, hashlib.sha256(body).hexdigest(), {ANONYMIZED_HEADER: "1"})
    )
    assert grant.expires_at == START + timedelta(minutes=15)
    assert grant.headers == {
        "content-type": VIDEO,
        CHECKSUM_HEADER: sha256_b64(body),
        ANONYMIZED_HEADER: "1",
    }
    response = httpx.put(
        grant.url, content=body, headers=dict(grant.headers), timeout=HTTP_TIMEOUT_SECONDS
    )
    assert response.status_code == 200, response.text
    reference = clip_reference(
        OWNER,
        clip_id=_clip_id(200),
        content_type=VIDEO,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )
    checks = _verify(storage, [reference])
    assert checks[0].ok


@pytest.mark.parametrize(
    "variant", ["sin_suma", "otra_suma", "sin_marca", "marca_0", "otro_tipo", "bytes_cambiados"]
)
def test_presign_put_rejects_missing_or_other_headers(
    storage: S3Storage, s3: Any, bucket: str, variant: str
) -> None:
    n = 300 + [
        "sin_suma",
        "otra_suma",
        "sin_marca",
        "marca_0",
        "otro_tipo",
        "bytes_cambiados",
    ].index(variant)
    body = _body(n)
    key = _key(n)
    grant = asyncio.run(
        storage.presign_put(key, VIDEO, hashlib.sha256(body).hexdigest(), {ANONYMIZED_HEADER: "1"})
    )
    headers = dict(grant.headers)
    content = body
    if variant == "sin_suma":
        del headers[CHECKSUM_HEADER]
    elif variant == "otra_suma":
        headers[CHECKSUM_HEADER] = sha256_b64(body + b"x")
    elif variant == "sin_marca":
        del headers[ANONYMIZED_HEADER]
    elif variant == "marca_0":
        headers[ANONYMIZED_HEADER] = "0"
    elif variant == "otro_tipo":
        headers["content-type"] = "image/jpeg"
    else:
        content = body[:-1] + bytes([body[-1] ^ 1])
    response = httpx.put(grant.url, content=content, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    assert response.status_code in (400, 403), response.text
    assert "<Code>SignatureDoesNotMatch</Code>" in response.text or (
        "<Code>BadDigest</Code>" in response.text
    )
    assert asyncio.run(storage.head_object(key)) is None


def test_presign_get_reads_the_object(storage: S3Storage, s3: Any, bucket: str) -> None:
    body = _body(400)
    key = _key(400)
    s3.put_object(Bucket=bucket, Key=key, Body=body, ChecksumSHA256=sha256_b64(body))
    grant = asyncio.run(storage.presign_get(key))
    assert grant.expires_at == START + timedelta(minutes=5)
    response = httpx.get(grant.url, timeout=HTTP_TIMEOUT_SECONDS)
    assert response.status_code == 200
    assert response.content == body
    assert asyncio.run(storage.get_object(key)) == body


def test_put_object_stores_checksum_and_encryption(
    storage: S3Storage, s3: Any, bucket: str
) -> None:
    body = b'{"archivo": "sintetico"}'
    head = asyncio.run(storage.put_object("archive/t112/lote.json", body, "application/json"))
    assert head.checksum_sha256 == sha256_b64(body)
    stored = s3.head_object(Bucket=bucket, Key="archive/t112/lote.json", ChecksumMode="ENABLED")
    assert stored["ChecksumSHA256"] == sha256_b64(body)
    assert stored["ServerSideEncryption"] == "aws:kms"


# --- Subida por partes -------------------------------------------------------------------------


def _put_part(storage: S3Storage, key: str, upload_id: str, number: int, data: bytes) -> str:
    grant = asyncio.run(
        storage.presign_part(key, upload_id, number, hashlib.sha256(data).hexdigest())
    )
    response = httpx.put(
        grant.url, content=data, headers=dict(grant.headers), timeout=HTTP_TIMEOUT_SECONDS
    )
    assert response.status_code == 200, response.text
    return response.headers["etag"]


def test_multipart_upload_yields_object_whose_checksum_head_returns(storage: S3Storage) -> None:
    key = "org/t112/closure/adjunto.pdf"
    parts = [_body(500, 5 * 1024 * 1024), _body(501, 1234)]
    upload_id = asyncio.run(storage.create_multipart(key, "application/pdf"))

    # Una parte con bytes que no son los de su suma firmada la rechaza el almacén.
    bad = asyncio.run(storage.presign_part(key, upload_id, 2, hashlib.sha256(parts[1]).hexdigest()))
    rejected = httpx.put(
        bad.url, content=parts[1] + b"x", headers=dict(bad.headers), timeout=HTTP_TIMEOUT_SECONDS
    )
    assert rejected.status_code == 400
    assert "<Code>BadDigest</Code>" in rejected.text

    completed = [
        CompletedPart(
            n, _put_part(storage, key, upload_id, n, data), hashlib.sha256(data).hexdigest()
        )
        for n, data in enumerate(parts, start=1)
    ]
    head = asyncio.run(storage.complete_multipart(key, upload_id, completed))
    composite = hashlib.sha256(b"".join(hashlib.sha256(p).digest() for p in parts)).digest()
    assert head.checksum_type is ChecksumType.COMPOSITE
    assert head.checksum_sha256 == base64.b64encode(composite).decode("ascii") + "-2"
    assert head.full_object_sha256_hex is None  # una suma compuesta no verifica una evidencia
    again = asyncio.run(storage.head_object(key))
    assert again is not None and again.checksum_sha256 == head.checksum_sha256
    assert asyncio.run(storage.get_object(key)) == b"".join(parts)


def test_aborted_multipart_accepts_no_more_parts(storage: S3Storage) -> None:
    key = "org/t112/closure/abortado.pdf"
    upload_id = asyncio.run(storage.create_multipart(key, "application/pdf"))
    asyncio.run(storage.abort_multipart(key, upload_id))
    data = _body(600, 100)
    grant = asyncio.run(storage.presign_part(key, upload_id, 1, hashlib.sha256(data).hexdigest()))
    response = httpx.put(
        grant.url, content=data, headers=dict(grant.headers), timeout=HTTP_TIMEOUT_SECONDS
    )
    assert response.status_code == 404
    assert "<Code>NoSuchUpload</Code>" in response.text
    assert asyncio.run(storage.head_object(key)) is None


# --- Almacén detenido o congelado --------------------------------------------------------------


@pytest.fixture
def dedicated_localstack() -> Iterator[tuple[Any, str]]:
    """LocalStack propio de la prueba, que la prueba puede congelar o detener.

    No es el de la sesión (ni el de ``docker compose``): detenerlo no afecta a otras pruebas.
    """
    from testcontainers.core.container import DockerContainer

    def build() -> DockerContainer:
        container = DockerContainer(LOCALSTACK_IMAGE).with_exposed_ports(LOCALSTACK_PORT)
        for name, value in LOCALSTACK_ENVIRONMENT.items():
            container.with_env(name, value)
        return container

    container = _start(build)
    try:
        url = (
            f"http://{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(LOCALSTACK_PORT)}"
        )
        _wait_for_localstack(url, timeout=STARTUP_TIMEOUT_SECONDS)
        yield container, url
    finally:
        with contextlib.suppress(Exception):
            container.get_wrapped_container().unpause()
        with contextlib.suppress(Exception):
            container.stop()


def _prepared(url: str) -> tuple[S3Storage, list[ClipReference]]:
    endpoint = LocalStackEndpoint(url=url)
    s3 = endpoint.aws_client("s3")
    bucket = f"vigia-timeouts-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=bucket)
    clips = [StoredClip(_clip_id(700 + n), _body(700 + n)) for n in range(2)]
    refs = [clip.reference(Tamper.NONE) for clip in clips]
    for clip, ref in zip(clips, refs, strict=True):
        s3.put_object(
            Bucket=bucket,
            Key=ref.storage_key,
            Body=clip.body,
            ChecksumSHA256=sha256_b64(clip.body),
            Metadata={"vigia-anonymized": "1"},
        )
    storage = S3Storage(endpoint.storage_settings(bucket), SimulatedClock(START))
    assert all(check.ok for check in _verify(storage, refs))
    return storage, refs


def _time_until_transient(storage: S3Storage, refs: list[ClipReference]) -> float:
    started = WALL_CLOCK.monotonic()
    with pytest.raises(StorageUnavailable) as raised:
        _verify(storage, refs)
    elapsed = WALL_CLOCK.monotonic() - started
    assert raised.value.code == "storage_unavailable"
    assert raised.value.retryable is True
    return elapsed


def test_stopped_localstack_ends_in_transient_error(
    dedicated_localstack: tuple[Any, str], capsys: pytest.CaptureFixture[str]
) -> None:
    container, url = dedicated_localstack
    storage, refs = _prepared(url)
    container.stop()
    elapsed = _time_until_transient(storage, refs)
    with capsys.disabled():
        print(f"\nLocalStack detenido: storage_unavailable en {elapsed:.3f} s")
    assert elapsed < storage_timeout_bound()


def test_frozen_localstack_ends_at_its_read_timeout(
    dedicated_localstack: tuple[Any, str], capsys: pytest.CaptureFixture[str]
) -> None:
    container, url = dedicated_localstack
    storage, refs = _prepared(url)
    container.get_wrapped_container().pause()
    elapsed = _time_until_transient(storage, refs)
    with capsys.disabled():
        print(
            f"\nLocalStack congelado: storage_unavailable en {elapsed:.3f} s "
            "(lectura 10 s, tope de la llamada 16 s)"
        )
    # Las dos consultas corren en paralelo: termina en un tiempo de espera, no en dos.
    assert 9.0 <= elapsed < storage_timeout_bound()


def storage_timeout_bound() -> float:
    """Tope de una llamada (conexión 5 s + lectura 10 s + 1 s) más holgura de arranque."""
    return StorageSettings(bucket="x").call_timeout_seconds + 1.0
