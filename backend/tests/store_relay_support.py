"""Dobles del relevo firmado del almacén (TASK-607): un S3 en memoria con versiones y la app.

``FakeS3`` imita lo que el relevo usa de boto3 (``head_object``, ``get_object`` con rango y
versión, ``put_object`` con la suma) y **cuenta cada llamada**: así se comprueba que un token
rechazado no llega al almacén. Solo datos generados.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from botocore.exceptions import ClientError

from tests.api_support import World
from vigia_platform.shared.api.app import UnitRegistration
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.storage import StorageSettings
from vigia_platform.shared.storage_relay import StoreRelaySettings, StoreRelayStorage

__all__ = [
    "BUCKET",
    "EDGE_BUCKET",
    "KEY",
    "ORIGIN",
    "START",
    "FakeS3",
    "RecordingCounter",
    "attachment_key",
    "clip_key",
    "relay_app",
    "relay_storage",
    "same_origin",
    "send",
    "target",
]

BUCKET: Final = "vigia-evidence"
EDGE_BUCKET: Final = "vigia-edge"
ARCHIVE_BUCKET: Final = "vigia-archive"
ORIGIN: Final = "https://app.vigia.test:8443"
KEY: Final = bytes(range(32))
START: Final = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
"""Lejos de la hora real: un reloj del sistema en vez del inyectado cambia el resultado."""


def clip_key() -> str:
    """Clave de un clip del nodo (``node/``): nunca se sube por el relevo (A-108)."""
    return f"org/{uuid.UUID(int=1)}/plant/{uuid.UUID(int=2)}/node/{uuid.UUID(int=3)}/c.mp4"


def attachment_key(attachment: int = 9, extension: str = "png") -> str:
    """Clave de un adjunto de cierre con la forma de ``CLOSURE_KEY_PATTERN`` (BR-LAZ-44)."""
    ids = [uuid.UUID(int=n) for n in (1, 2, 3, 4, attachment)]
    return f"org/{ids[0]}/plant/{ids[1]}/zone/{ids[2]}/closure/{ids[3]}/{ids[4]}.{extension}"


def _error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, code
    )


@dataclass
class _Version:
    version_id: str
    body: bytes
    content_type: str
    metadata: Mapping[str, str]


class _Body:
    """``StreamingBody`` reducido: ``read(n)`` y ``close``."""

    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.closed = False

    def read(self, amount: int) -> bytes:
        return self._stream.read(amount)

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeS3:
    """S3 versionado en memoria con las respuestas de error de boto3."""

    objects: dict[tuple[str, str], list[_Version]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def store(self, bucket: str, key: str, body: bytes, content_type: str = "video/mp4") -> str:
        with self.lock:
            versions = self.objects.setdefault((bucket, key), [])
            version = _Version(f"v{len(versions) + 1}", body, content_type, {})
            versions.append(version)
            return version.version_id

    def _find(self, bucket: str, key: str, version_id: str | None) -> _Version:
        versions = self.objects.get((bucket, key))
        if not versions:
            raise _error("NoSuchKey", 404)
        if version_id is None:
            return versions[-1]
        for version in versions:
            if version.version_id == version_id:
                return version
        raise _error("NoSuchVersion", 404)

    def head_object(self, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:
        self.calls.append("head_object")
        version = self._find(Bucket, Key, VersionId)
        return {
            "ContentLength": len(version.body),
            "ContentType": version.content_type,
            "ETag": f'"{version.version_id}"',
            "VersionId": version.version_id,
        }

    def get_object(
        self,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
        Range: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append("get_object")
        body = self._find(Bucket, Key, VersionId).body
        if Range is not None:
            first, last = (int(n) for n in Range.removeprefix("bytes=").split("-"))
            body = body[first : last + 1]
        return {"Body": _Body(body)}

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ChecksumSHA256: str,
        Metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        self.calls.append("put_object")
        digest = base64.b64encode(hashlib.sha256(Body).digest()).decode()
        if digest != ChecksumSHA256:
            raise _error("BadDigest", 400)
        with self.lock:
            versions = self.objects.setdefault((Bucket, Key), [])
            version = _Version(f"v{len(versions) + 1}", Body, ContentType, dict(Metadata))
            versions.append(version)
        return {"ETag": f'"{version.version_id}"', "VersionId": version.version_id}


class RecordingCounter:
    """``store_relay_requests_total`` que guarda cada suma con sus atributos."""

    def __init__(self) -> None:
        self.added: list[dict[str, object]] = []

    def add(self, amount: int | float = 1, attributes: Mapping[str, object] | None = None) -> None:
        self.added.append(dict(attributes or {}))


class _Metrics:
    def __init__(self) -> None:
        self.store_relay_requests_total = RecordingCounter()


def relay_storage(
    client: Any,
    clock: SimulatedClock,
    *,
    bucket: str = BUCKET,
    settings: StorageSettings | None = None,
    metrics: Any | None = None,
) -> StoreRelayStorage:
    """El relevo de ``bucket`` con ``client`` (``FakeS3`` o el de LocalStack)."""
    relay = StoreRelaySettings(
        key=KEY,
        public_origin=ORIGIN,
        metrics=metrics if metrics is not None else _Metrics(),  # type: ignore[arg-type]
    )
    return StoreRelayStorage(
        settings if settings is not None else StorageSettings(bucket=bucket),
        clock,
        relay=relay,
        refused_buckets=frozenset({EDGE_BUCKET, ARCHIVE_BUCKET}),
        client=client,
    )


def relay_app(
    world: World, relay: StoreRelayStorage | None, environment: str = "test", **runtime: Any
) -> Any:
    """La aplicación real (cadena entera, sin unidades) con o sin el relevo."""
    units: tuple[UnitRegistration, ...] = (UnitRegistration("shared"),)
    return world.app(
        environment,
        units=units,
        permissions=frozenset(),
        runtime={"store_relay": relay, **runtime},
        public_origin=ORIGIN,
    )


async def send(app: Any, method: str, url: str, **options: Any) -> httpx.Response:
    """Una petición a ``app`` en proceso (sin red), con la ruta y la consulta de ``url``."""
    transport = httpx.ASGITransport(app=app, client=("100.64.0.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, timeout=30.0) as client:
        return await client.request(method, target(url), **options)


def target(url: str) -> str:
    """La ruta y la consulta de una URL del relevo (la parte que llega a la API)."""
    parsed = httpx.URL(url)
    return parsed.raw_path.decode("ascii")


def same_origin() -> dict[str, str]:
    """Las cabeceras que pone el navegador en un ``PUT`` del mismo origen."""
    return {"sec-fetch-site": "same-origin", "origin": ORIGIN}


def metrics_of(relay: StoreRelayStorage) -> PlatformMetrics:
    return relay._metrics  # type: ignore[return-value]
