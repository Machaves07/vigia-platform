"""Estrategias de Hypothesis para evidencias: dueños, UUID v7 y ``ClipReference`` sintéticos.

Solo datos generados (NFR-CTR-43): los bytes de los clips son aleatorios, nunca imágenes reales.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import strategies as st
from vigia_contracts.models.clip_reference import ClipReference, ContentType

from vigia_platform.ledger.evidence import (
    EXTENSION_BY_CONTENT_TYPE,
    EvidenceOwner,
    StorageKey,
    format_storage_key,
)

VIDEO = "video/mp4"
IMAGE = "image/jpeg"
_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)


def uuid7_from_bits(bits: int) -> uuid.UUID:
    """UUID v7 con los 122 bits libres tomados de ``bits`` (versión 7, variante RFC 9562)."""
    value = bits & ((1 << 128) - 1)
    value &= ~(0xF << 76)
    value |= 0x7 << 76
    value &= ~(0x3 << 62)
    value |= 0x2 << 62
    return uuid.UUID(int=value)


def uuid7s() -> st.SearchStrategy[uuid.UUID]:
    return st.integers(min_value=0, max_value=(1 << 128) - 1).map(uuid7_from_bits)


def owners() -> st.SearchStrategy[EvidenceOwner]:
    ids = st.uuids(version=4)
    return st.builds(EvidenceOwner, ids, ids, ids, ids)


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def clip_reference(
    owner: EvidenceOwner,
    *,
    clip_id: uuid.UUID,
    content_type: str,
    sha256: str,
    size_bytes: int,
    storage_key: str | None = None,
    offset_ms: int = 0,
) -> ClipReference:
    """Una ``ClipReference`` válida del contrato (validación estricta, desde JSON)."""
    key = storage_key or format_storage_key(
        StorageKey(
            owner.organization_id,
            owner.plant_id,
            owner.zone_id,
            owner.node_id,
            clip_id,
            EXTENSION_BY_CONTENT_TYPE[ContentType(content_type)],
        )
    )
    document: dict[str, Any] = {
        "clip_id": str(clip_id),
        "camera_id": str(uuid.UUID(int=clip_id.int ^ 0x5A5A, version=4)),
        "media_kind": "video" if content_type == VIDEO else "image",
        "content_type": content_type,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "segment": "full",
        "anonymized": True,
        "storage_key": key,
    }
    if content_type == VIDEO:
        starts = _EPOCH + timedelta(milliseconds=offset_ms)
        document |= {
            "duration_ms": 10_000,
            "starts_at": _timestamp(starts),
            "ends_at": _timestamp(starts + timedelta(seconds=10)),
        }
    return ClipReference.model_validate_json(json.dumps(document))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@st.composite
def clip_references(draw: st.DrawFn) -> tuple[EvidenceOwner, ClipReference]:
    """Un dueño y una referencia coherente con él."""
    owner = draw(owners())
    reference = clip_reference(
        owner,
        clip_id=draw(uuid7s()),
        content_type=draw(st.sampled_from([VIDEO, IMAGE])),
        sha256=draw(st.binary(min_size=32, max_size=32)).hex(),
        size_bytes=draw(st.integers(min_value=1, max_value=52_428_800)),
        offset_ms=draw(st.integers(min_value=0, max_value=10**9)),
    )
    return owner, reference
