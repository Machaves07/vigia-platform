"""El relevo firmado del almacén contra LocalStack (TASK-607, A-130; O-1, O-15).

La aplicación real (``create_app`` con la cadena entera) con ``StoreRelayStorage`` sobre un
depósito versionado de LocalStack, como ``vigia-evidence`` en la integración. Solo bytes
sintéticos (``tests.attachment_support``).

- **Clip legible**: la URL de lectura (la de ``EvidenceService.url_lectura``: ``head_object`` y
  ``presign_get`` con la versión verificada) es del mismo origen; ``Range: bytes=0-1023`` da 206
  con esos bytes exactos y sin rango, 200 con la suma guardada; con una segunda versión de la misma
  clave, la URL sigue sirviendo la primera.
- **Adjunto de ida y vuelta**: concesión de U-04 → ``PUT`` del navegador al relevo con las
  cabeceras firmadas → verificación de U-04 (versión fijada) → lectura como descarga. Otro tipo,
  otra suma o sin una cabecera: 403; otros bytes: 400 y ninguna versión.
- **Concurrencia**: dos ``PUT`` simultáneos con la misma URL terminan en 200 (o en un transitorio)
  y la versión vigente tiene los bytes firmados; un ``GET`` a la vez que un ``PUT`` devuelve una
  versión entera.
- **Registros**: ni el token ni la clave del objeto aparecen en ninguna línea de la API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.api_support import World
from tests.attachment_support import jpeg, png, tiff, utc
from tests.authz_support import sealed_context
from tests.integration.conftest import LocalStackEndpoint, versioned_bucket
from tests.integration.test_loop_attachments_localstack import Flow, _flow, _subject
from tests.store_relay_support import ORIGIN, relay_app, relay_storage, same_origin, send
from vigia_platform.loop.adapters.attachments.repository import StoredAttachment
from vigia_platform.loop.adapters.attachments.verify import AttachmentClaim
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import AllowedScope, Role, ScopeLevel
from vigia_platform.shared.observability.logging import JsonFormatter
from vigia_platform.shared.observability.redaction import AttributePolicy
from vigia_platform.shared.storage import sha256_b64
from vigia_platform.shared.storage_relay import RELAY_PATH, StoreRelayStorage

pytestmark = pytest.mark.integration

CLIP = bytes((n * 7 + n // 256) % 256 for n in range(200_000))
"""Un «clip» sintético de 200 KB: bytes deterministas, sin vídeo real."""


@pytest.fixture(scope="module")
def s3(localstack_endpoint: LocalStackEndpoint) -> Any:
    return localstack_endpoint.aws_client("s3")


@pytest.fixture(scope="module")
def bucket(s3: Any) -> Iterator[str]:
    with versioned_bucket(s3, "vigia-evidence-relay") as name:
        yield name


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(utc())


@pytest.fixture
def relay(
    localstack_endpoint: LocalStackEndpoint, bucket: str, clock: SimulatedClock
) -> StoreRelayStorage:
    settings = localstack_endpoint.storage_settings(bucket)
    return relay_storage(None, clock, settings=settings)


@pytest.fixture
def app(relay: StoreRelayStorage, clock: SimulatedClock) -> Any:
    return relay_app(World(clock=clock), relay, environment="integration")


def _clip_key() -> str:
    ids = [uuid.uuid4() for _ in range(5)]
    return f"org/{ids[0]}/plant/{ids[1]}/zone/{ids[2]}/node/{ids[3]}/{ids[4]}.mp4"


def _put(s3: Any, bucket: str, key: str, body: bytes) -> str:
    response = s3.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="video/mp4", ChecksumSHA256=sha256_b64(body)
    )
    return str(response["VersionId"])


def _read_url(relay: StoreRelayStorage, key: str) -> str:
    """Lo que hace ``EvidenceService.url_lectura``: la versión vigente verificada, fijada."""
    head = asyncio.run(relay.head_object(key))
    assert head is not None and head.version_id is not None
    return asyncio.run(relay.presign_get(key, version_id=head.version_id)).url


# --- Clip legible --------------------------------------------------------------------------------


def test_a_clip_is_served_by_range_and_whole_from_the_same_origin(
    s3: Any, bucket: str, relay: StoreRelayStorage, app: Any
) -> None:
    key = _clip_key()
    _put(s3, bucket, key, CLIP)
    url = _read_url(relay, key)
    parsed = httpx.URL(url)
    assert (f"{parsed.scheme}://{parsed.host}:{parsed.port}", parsed.path) == (ORIGIN, RELAY_PATH)
    ranged = asyncio.run(send(app, "GET", url, headers={"range": "bytes=0-1023"}))
    assert ranged.status_code == 206
    assert ranged.content == CLIP[:1024]
    assert ranged.headers["content-range"] == f"bytes 0-1023/{len(CLIP)}"
    whole = asyncio.run(send(app, "GET", url))
    assert whole.status_code == 200
    assert hashlib.sha256(whole.content).hexdigest() == hashlib.sha256(CLIP).hexdigest()
    assert whole.headers["content-type"] == "video/mp4"


def test_the_url_keeps_serving_the_verified_version_after_a_new_one(
    s3: Any, bucket: str, relay: StoreRelayStorage, app: Any
) -> None:
    key = _clip_key()
    _put(s3, bucket, key, CLIP)
    url = _read_url(relay, key)
    _put(s3, bucket, key, b"segunda version de la misma clave")
    whole = asyncio.run(send(app, "GET", url))
    ranged = asyncio.run(send(app, "GET", url, headers={"range": "bytes=0-9"}))
    assert (whole.status_code, whole.content) == (200, CLIP)
    assert (ranged.status_code, ranged.content) == (206, CLIP[:10])


# --- Adjunto de cierre de ida y vuelta ---------------------------------------------------------


def _relay_upload(flow: Flow, app: Any, body: bytes, content_type: str) -> AttachmentClaim:
    issued = asyncio.run(
        flow.service.grant_upload(
            flow.context(),
            flow.action,
            content_type=content_type,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    )
    assert httpx.URL(issued.upload.url).path == RELAY_PATH
    headers = {**dict(issued.upload.headers), **same_origin()}
    put = asyncio.run(send(app, "PUT", issued.upload.url, content=body, headers=headers))
    assert put.status_code == 200, put.text
    assert put.headers["etag"]
    return AttachmentClaim(
        issued.grant.attachment_id, content_type, len(body), issued.grant.sha256, "evidencia", True
    )


def test_a_closure_attachment_round_trips_through_the_relay(
    relay: StoreRelayStorage, clock: SimulatedClock, app: Any
) -> None:
    flow = _flow(relay, clock)
    body = jpeg(exif=tiff(gps_tags=None))
    claim = _relay_upload(flow, app, body, "image/jpeg")
    (verified,) = asyncio.run(flow.service.verify(_subject(flow), [claim]))
    assert verified.version_id is not None
    flow.repository.attachments[verified.attachment_id] = StoredAttachment(
        flow.place.organization_id,
        flow.place.plant_id,
        flow.place.zone_id,
        verified.storage_key,
        verified.sha256,
        verified.size_bytes,
    )
    reader = sealed_context(
        flow.place.organization_id,
        [AllowedScope(ScopeLevel.ZONE, flow.place.zone_id, Role.PLANT_MANAGER)],
    )
    grant = asyncio.run(flow.service.read_url(reader, verified.attachment_id))
    assert httpx.URL(grant.url).path == RELAY_PATH
    response = asyncio.run(send(app, "GET", grant.url))
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("variant", ["other_type", "other_sum", "missing_sum", "other_bytes"])
def test_an_upload_outside_what_was_signed_stores_nothing(
    s3: Any, bucket: str, relay: StoreRelayStorage, clock: SimulatedClock, app: Any, variant: str
) -> None:
    flow = _flow(relay, clock)
    body = png(texts=((b"tEXt", b"Comment\x00" + variant.encode()),))
    issued = asyncio.run(
        flow.service.grant_upload(
            flow.context(),
            flow.action,
            content_type="image/png",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    )
    headers = {**dict(issued.upload.headers), **same_origin()}
    content = body
    if variant == "other_type":
        headers["content-type"] = "image/jpeg"
    elif variant == "other_sum":
        headers["x-amz-checksum-sha256"] = base64.b64encode(bytes(32)).decode()
    elif variant == "missing_sum":
        del headers["x-amz-checksum-sha256"]
    else:
        content = body + b"!"
    response = asyncio.run(send(app, "PUT", issued.upload.url, content=content, headers=headers))
    assert response.status_code == (400 if variant == "other_bytes" else 403), response.text
    listed = s3.list_object_versions(Bucket=bucket, Prefix=issued.grant.storage_key)
    assert listed.get("Versions", []) == []  # la prueba sí lista: ninguna versión nueva


# --- Concurrencia --------------------------------------------------------------------------------


def test_two_simultaneous_uploads_and_a_read_never_mix_bytes(
    s3: Any, bucket: str, relay: StoreRelayStorage, clock: SimulatedClock, app: Any
) -> None:
    flow = _flow(relay, clock)
    body = png(texts=((b"tEXt", b"Comment\x00" + b"x" * 50_000),))
    sha = hashlib.sha256(body).hexdigest()
    issued = asyncio.run(
        flow.service.grant_upload(
            flow.context(), flow.action, content_type="image/png", size_bytes=len(body), sha256=sha
        )
    )
    headers = {**dict(issued.upload.headers), **same_origin()}
    key = issued.grant.storage_key

    async def two_puts() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(send(app, "PUT", issued.upload.url, content=body, headers=headers) for _ in "ab")
            )
        )

    statuses = [response.status_code for response in asyncio.run(two_puts())]
    assert set(statuses) <= {200, 503}, statuses
    assert 200 in statuses
    current = asyncio.run(relay.head_object(key))
    assert current is not None and current.full_object_sha256_hex == sha
    read_url = asyncio.run(relay.presign_get(key, version_id=current.version_id)).url

    async def read_while_writing() -> tuple[httpx.Response, httpx.Response]:
        read, write = await asyncio.gather(
            send(app, "GET", read_url),
            send(app, "PUT", issued.upload.url, content=body, headers=headers),
        )
        return read, write

    read, write = asyncio.run(read_while_writing())
    assert write.status_code in (200, 503)
    assert read.status_code == 200
    assert hashlib.sha256(read.content).hexdigest() == sha
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == body


# --- Registros -----------------------------------------------------------------------------------


def test_no_api_log_line_carries_the_token_or_the_object_key(
    s3: Any,
    bucket: str,
    relay: StoreRelayStorage,
    clock: SimulatedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    policy = AttributePolicy()  # la de la aplicación: con sus rutas registradas, como en producción
    app = relay_app(World(clock=clock), relay, environment="integration", attribute_policy=policy)
    key = _clip_key()
    _put(s3, bucket, key, CLIP)
    read_url = _read_url(relay, key)
    flow = _flow(relay, clock)
    body = png()
    issued = asyncio.run(
        flow.service.grant_upload(
            flow.context(),
            flow.action,
            content_type="image/png",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    )
    caplog.clear()
    assert asyncio.run(send(app, "GET", read_url)).status_code == 200
    headers = {**dict(issued.upload.headers), **same_origin()}
    put = asyncio.run(send(app, "PUT", issued.upload.url, content=body, headers=headers))
    assert put.status_code == 200
    denied = asyncio.run(send(app, "GET", read_url + "x"))
    assert denied.status_code == 403
    # Las líneas del cliente de prueba (httpx, que registra la URL que pide) no son de la API.
    records = [r for r in caplog.records if r.name.split(".")[0] not in {"httpx", "httpcore"}]
    formatter = JsonFormatter(policy)
    formatted = [formatter.format(record) for record in records]
    routes = [json.loads(line).get("route") for line in formatted]
    assert routes.count(RELAY_PATH) == 3  # sí hay registro: una línea por petición, sin consulta
    lines = formatted + [record.getMessage() for record in records]
    secrets = [
        httpx.URL(read_url).params["token"],
        httpx.URL(issued.upload.url).params["token"],
        key,
        issued.grant.storage_key,
    ]
    for line in lines:
        for secret in secrets:
            assert secret not in line
