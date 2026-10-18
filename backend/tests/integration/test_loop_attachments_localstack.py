"""Adjuntos de cierre contra LocalStack, solo con bytes sintéticos."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from vigia_contracts.clock import SimulatedClock

from tests.integration.conftest import LocalStackEndpoint, versioned_bucket
from vigia_platform.loop.adapters.attachments.grants import (
    AttachmentUploadGrant,
    ClosureObjectStore,
)
from vigia_platform.shared.storage import S3Storage

START = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture(scope="module")
def s3(localstack_endpoint: LocalStackEndpoint) -> Any:
    return localstack_endpoint.aws_client("s3")


@pytest.fixture(scope="module")
def bucket(s3: Any) -> Iterator[str]:
    with versioned_bucket(s3, "vigia-evidence-attachments") as name:
        yield name


@pytest.fixture(scope="module")
def storage(localstack_endpoint: LocalStackEndpoint, bucket: str) -> S3Storage:
    return S3Storage(localstack_endpoint.storage_settings(bucket), SimulatedClock(START))


@pytest.mark.integration
def test_presigned_put_requires_the_signed_checksum(storage: S3Storage) -> None:
    body = b"\x89PNG\r\n\x1a\nsynthetic"
    digest = hashlib.sha256(body).hexdigest()
    clock = SimulatedClock(START)
    grant = AttachmentUploadGrant.issue(
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        zone_id=uuid.uuid4(),
        subject_id=uuid.uuid4(),
        content_type="image/png",
        size_bytes=len(body),
        sha256=digest,
        issued_at=START,
        clock=clock,
        random_bytes=lambda _: b"\0" * 16,
    )
    signed = asyncio.run(ClosureObjectStore(storage).grant(grant, START))
    ok = httpx.put(signed.url, content=body, headers=dict(signed.headers), timeout=30.0)
    assert ok.status_code in (200, 204)
    assert asyncio.run(storage.head_object(grant.storage_key)) is not None
