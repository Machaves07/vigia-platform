"""Adjuntos de cierre contra LocalStack, con la URL que firma la plataforma (A-47; TASK-310).

Solo bytes sintéticos (``tests.attachment_support``). La política de ``vigia-api-task``
(``s3:PutObject`` sobre ``org/*/closure/*`` con condición de suma, U04-H-11) no se reproduce:
LocalStack comunitario no evalúa IAM; lo que se prueba aquí es lo que hace el propio almacén con
la URL firmada.

- PR-LAZ-63: un ``PUT`` con la suma firmada entra; sin la suma, con otra suma o con otros bytes,
  LocalStack lo rechaza y no queda objeto.
- Recorrido completo: concesión → ``PUT`` del navegador → verificación (versión fijada) →
  lectura con ``Content-Disposition: attachment``.
- Una imagen con GPS y un PNG renombrado a PDF, ya subidos, se rechazan sin tocar sus bytes.
- Almacén detenido (intermediario que rechaza conexiones): la concesión declara
  ``StorageUnavailable``, el circuito queda abierto sin volver a llamar, y ``/health/ready`` (su
  sonda real) no cambia por ello.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from tests.attachment_support import (
    DeniedLog,
    MemoryRepository,
    RecordingAudit,
    jpeg,
    png,
    tiff,
    utc,
)
from tests.authz_support import sealed_context
from tests.fault_proxy import ProxyMode, fault_proxy
from tests.integration.conftest import LocalStackEndpoint, versioned_bucket
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.loop.adapters.attachments.grants import (
    AttachmentUploadGrant,
    ClosureUploadSigner,
)
from vigia_platform.loop.adapters.attachments.repository import ActionPlace, StoredAttachment
from vigia_platform.loop.adapters.attachments.rules import AttachmentRejected
from vigia_platform.loop.adapters.attachments.store import StoreCircuit
from vigia_platform.loop.adapters.attachments.verify import AttachmentClaim, AttachmentSubject
from vigia_platform.loop.application.attachments import (
    ClosureAttachmentService,
    build_attachment_service,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.api.health import ReadinessProbe
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import AllowedScope, Role, ScopeLevel
from vigia_platform.shared.db import DatabaseHealth
from vigia_platform.shared.outbox.breaker import CircuitState
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.storage import S3Storage, StorageSettings, StorageUnavailable

pytestmark = pytest.mark.integration

SENTINEL = "health/sentinel"
HTTP_TIMEOUT = 30.0


@pytest.fixture(scope="module")
def s3(localstack_endpoint: LocalStackEndpoint) -> Any:
    return localstack_endpoint.aws_client("s3")


@pytest.fixture(scope="module")
def bucket(s3: Any) -> Iterator[str]:
    with versioned_bucket(s3, "vigia-evidence-attachments") as name:
        s3.put_object(Bucket=name, Key=SENTINEL, Body=b"centinela")
        yield name


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(utc())


@pytest.fixture
def storage(
    localstack_endpoint: LocalStackEndpoint, bucket: str, clock: SimulatedClock
) -> S3Storage:
    return S3Storage(localstack_endpoint.storage_settings(bucket), clock)


@dataclass
class Flow:
    service: ClosureAttachmentService
    repository: MemoryRepository
    audit: RecordingAudit
    place: ActionPlace
    action: uuid.UUID

    def context(self) -> Any:
        return sealed_context(
            self.place.organization_id,
            [AllowedScope(ScopeLevel.ZONE, self.place.zone_id, Role.COORDINATOR_SST)],
        )


def _flow(storage: S3Storage, clock: SimulatedClock) -> Flow:
    repository, audit = MemoryRepository(), RecordingAudit()
    place = ActionPlace(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    action = uuid.uuid4()
    repository.actions[action] = place
    service = build_attachment_service(
        repository=repository,
        storage=storage,
        authorizer=Authorizer(audit=DeniedLog(), provider_organization_id=uuid.uuid4()),
        audit=audit,
        clock=clock,
    )
    return Flow(service, repository, audit, place, action)


def _grant(storage: S3Storage, body: bytes, content_type: str = "image/png") -> Any:
    clock = SimulatedClock(utc())
    grant = AttachmentUploadGrant.issue(
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        zone_id=uuid.uuid4(),
        subject_id=uuid.uuid4(),
        content_type=content_type,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        clock=clock,
    )
    signed = asyncio.run(ClosureUploadSigner(storage, StoreCircuit(clock), clock).sign(grant))
    return grant, signed


def test_pr_laz_63_a_put_with_the_signed_checksum_is_stored(storage: S3Storage) -> None:
    body = png()
    grant, signed = _grant(storage, body)
    ok = httpx.put(signed.url, content=body, headers=dict(signed.headers), timeout=HTTP_TIMEOUT)
    assert ok.status_code in (200, 204), ok.text
    head = asyncio.run(storage.head_object(grant.storage_key))
    assert head is not None
    assert head.full_object_sha256_hex == grant.sha256
    assert head.content_type == "image/png"


@pytest.mark.parametrize("variant", ["missing", "other_sum", "other_bytes"])
def test_pr_laz_63_a_put_without_the_sum_or_with_another_is_rejected(
    storage: S3Storage, variant: str
) -> None:
    body = png(texts=((b"tEXt", b"Comment\x00" + variant.encode()),))
    grant, signed = _grant(storage, body)
    headers = dict(signed.headers)
    content = body
    if variant == "missing":
        del headers["x-amz-checksum-sha256"]
    elif variant == "other_sum":
        headers["x-amz-checksum-sha256"] = "A" * 43 + "="
    else:
        content = body + b"!"
    response = httpx.put(signed.url, content=content, headers=headers, timeout=HTTP_TIMEOUT)
    assert response.status_code in (400, 403), response.text
    assert asyncio.run(storage.head_object(grant.storage_key)) is None


def _upload(flow: Flow, body: bytes, content_type: str) -> AttachmentClaim:
    issued = asyncio.run(
        flow.service.grant_upload(
            flow.context(),
            flow.action,
            content_type=content_type,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
    )
    put = httpx.put(
        issued.upload.url, content=body, headers=dict(issued.upload.headers), timeout=HTTP_TIMEOUT
    )
    assert put.status_code in (200, 204), put.text
    return AttachmentClaim(
        issued.grant.attachment_id,
        content_type,
        len(body),
        issued.grant.sha256,
        "evidencia",
        True,
    )


def _subject(flow: Flow) -> AttachmentSubject:
    place = flow.place
    return AttachmentSubject(place.organization_id, place.plant_id, place.zone_id, place.subject_id)


def test_grant_upload_verify_and_read_end_to_end(storage: S3Storage, clock: SimulatedClock) -> None:
    flow = _flow(storage, clock)
    body = jpeg(exif=tiff(gps_tags=None))
    claim = _upload(flow, body, "image/jpeg")
    (verified,) = asyncio.run(flow.service.verify(_subject(flow), [claim]))
    assert verified.version_id is not None
    assert "/closure/" in verified.storage_key
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
    response = httpx.get(grant.url, timeout=HTTP_TIMEOUT)
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-disposition"] == "attachment"
    assert response.headers["content-type"] == "application/octet-stream"
    assert len(flow.audit.successes()) == 1


@pytest.mark.parametrize(
    ("body", "content_type", "detail"),
    [
        (
            jpeg(exif=tiff(gps_tags=(1, 2, 3, 4))),
            "image/jpeg",
            LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED,
        ),
        (png(), "application/pdf", LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED),
    ],
    ids=["gps", "png-renamed-to-pdf"],
)
def test_an_uploaded_object_that_fails_is_rejected_and_left_untouched(
    storage: S3Storage,
    clock: SimulatedClock,
    s3: Any,
    bucket: str,
    body: bytes,
    content_type: str,
    detail: LoopDetailCode,
) -> None:
    flow = _flow(storage, clock)
    claim = _upload(flow, body, content_type)
    with pytest.raises(AttachmentRejected) as raised:
        asyncio.run(flow.service.verify(_subject(flow), [claim]))
    assert raised.value.detail is detail
    key = (
        f"org/{flow.place.organization_id}/plant/{flow.place.plant_id}/zone/{flow.place.zone_id}"
        f"/closure/{flow.place.subject_id}/{claim.attachment_id}."
    )
    listed = s3.list_object_versions(Bucket=bucket, Prefix=key)  # la prueba sí lista
    assert len(listed.get("Versions", [])) == 1  # sin limpiar ni reescribir
    stored = s3.get_object(Bucket=bucket, Key=listed["Versions"][0]["Key"])["Body"].read()
    assert stored == body
    assert flow.audit.entries == []


class _HealthyDatabase:
    async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
        return DatabaseHealth(visible_organizations=0, schema_version=MINIMUM_SCHEMA_VERSION)


class _Keys:
    def has_active_key(self, purpose: object) -> bool:
        return True


def test_a_stopped_store_is_declared_and_readiness_does_not_change(
    localstack_endpoint: LocalStackEndpoint, bucket: str, storage: S3Storage, clock: SimulatedClock
) -> None:
    host, port = localstack_endpoint.url.split("//", 1)[1].rsplit(":", 1)
    probe = ReadinessProbe(
        database=_HealthyDatabase(),
        storage=storage,
        sentinel_key=SENTINEL,
        signing=_Keys(),  # type: ignore[arg-type]
    )
    before = asyncio.run(probe.check())
    assert before.ready
    with fault_proxy(host, int(port)) as proxy:
        settings = localstack_endpoint.storage_settings(bucket)
        proxied = S3Storage(
            StorageSettings(
                bucket=settings.bucket,
                endpoint_url=proxy.url,
                region=settings.region,
                addressing_style=settings.addressing_style,
                credentials=settings.credentials,
            ),
            clock,
        )
        flow = _flow(proxied, clock)
        _upload(flow, png(), "image/png")  # el almacén responde
        proxy.set_mode(ProxyMode.REFUSE)
        with pytest.raises(StorageUnavailable):
            asyncio.run(
                flow.service.grant_upload(
                    flow.context(),
                    flow.action,
                    content_type="image/png",
                    size_bytes=1,
                    sha256="a" * 64,
                )
            )
        circuit = flow.service.signer.circuit
        assert circuit.state is CircuitState.OPEN
        with pytest.raises(StorageUnavailable) as again:
            asyncio.run(
                flow.service.grant_upload(
                    flow.context(),
                    flow.action,
                    content_type="image/png",
                    size_bytes=1,
                    sha256="a" * 64,
                )
            )
        assert again.value.operation == "closure_attachment_circuit_open"  # sin llamar
        assert asyncio.run(flow.service.verify(_subject(flow), [])) == ()  # FS-LAZ-03
        after = asyncio.run(probe.check())
        assert after.ready == before.ready
        proxy.set_mode(ProxyMode.FORWARD)
        clock.advance(60)
        _upload(flow, png(), "image/png")  # la sonda pasa y el circuito cierra
        assert circuit.state is CircuitState.CLOSED
