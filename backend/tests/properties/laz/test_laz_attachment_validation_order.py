"""Orden fijo de validación de adjuntos de cierre."""

from __future__ import annotations

import base64
import hashlib

import pytest

from vigia_platform.loop.adapters.attachments.verify import (
    AttachmentCandidate,
    AttachmentRejected,
    AttachmentRejection,
    verify_attachment,
)
from vigia_platform.shared.storage import ChecksumType, ObjectHead


def _candidate(**changes: object) -> AttachmentCandidate:
    data = b"\x89PNG\r\n\x1a\nsynthetic"
    digest = hashlib.sha256(data).hexdigest()
    head = ObjectHead(
        key="org/a/plant/b/zone/c/closure/d/e.png",
        size_bytes=len(data),
        checksum_sha256=base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
        checksum_type=ChecksumType.FULL_OBJECT,
        content_type="image/png",
        metadata={},
        version_id=None,
    )
    values: dict[str, object] = {
        "content_type": "image/png",
        "size_bytes": len(data),
        "sha256": digest,
        "declared_no_identifiable_person": True,
        "head": head,
        "header": data,
    }
    values.update(changes)
    return AttachmentCandidate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"content_type": "image/gif", "size_bytes": 99_999_999}, AttachmentRejection.TYPE),
        (
            {"header": b"not-an-imageGPSLatitude", "size_bytes": 99_999_999},
            AttachmentRejection.TYPE,
        ),
        (
            {"header": b"\x89PNG\r\n\x1a\nGPSLatitude", "size_bytes": 99_999_999},
            AttachmentRejection.METADATA,
        ),
    ],
)
def test_first_invalid_step_wins(changes: dict[str, object], expected: AttachmentRejection) -> None:
    with pytest.raises(AttachmentRejected) as raised:
        verify_attachment(_candidate(**changes))
    assert raised.value.reason is expected
