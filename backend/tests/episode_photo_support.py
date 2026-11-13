"""Fotos del episodio sintéticas para las pruebas de TASK-572 (A-112; NFR-CTR-43).

- ``jpeg``: un JPEG **sintético** y mínimo que ``vigia_contracts.images.jpeg_facts`` lee sin
  decodificar píxeles (``SOI``, la marca ``vigia_anonymized=1`` en un ``COM``, un ``SOF0`` con el
  ancho y el alto pedidos, un barrido y ``EOI``). Con sus variantes hostiles: sin la marca, con un
  ``APP1`` (EXIF), con un ``APP0`` ``JFIF`` admitido o uno ``JFXX``, con bytes tras ``EOI`` o que no
  es un JPEG. Ninguna imagen real: solo bytes construidos aquí.
- ``image``: el objeto ``image`` del contrato para unos bytes (``size_bytes``, ``sha256``, ancho,
  alto y ``data_base64`` coherentes), con la opción de declarar otra cosa.
- ``submission``: una ``EpisodePhotoSubmission`` válida del contrato v1.5.0 para una zona, con su
  imagen o con ``image_absent_reason``, validada con el lector estricto.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import struct
import uuid
from typing import Any, Final

from vigia_contracts.models import api
from vigia_contracts.versioning import CONTRACT_VERSION

from vigia_platform.shared.ids import uuid7_at
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "BLUR_VERSION",
    "MODEL_VERSION",
    "image",
    "jpeg",
    "submission",
]

MARK: Final = b"vigia_anonymized=1"
BLUR_VERSION: Final = "blur-v1"
MODEL_VERSION: Final = "yolov8n-2026.09"
SOFTWARE_VERSION: Final = "1.5.0"
VERSION: Final = str(CONTRACT_VERSION)

_JFIF16: Final = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
"""El único ``APP0`` admitido: ``JFIF`` de 16 bytes sin miniatura (el de OpenCV)."""
_JFXX: Final = b"\xff\xe0\x00\x0aJFXX\x00\x10\x00\x00"
"""``APP0`` ``JFXX``: lleva una miniatura sin difuminar (A-112, precisión del 11-11/12-11)."""
_EXIF: Final = b"\xff\xe1\x00\x10Exif\x00\x00MM\x00*\x00\x00\x00\x08"
"""``APP1`` EXIF mínimo (la miniatura de EXIF no está difuminada)."""


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", 2 + len(payload)) + payload


def jpeg(
    width: int = 320,
    height: int = 240,
    *,
    marked: bool = True,
    app0: bytes | None = None,
    exif: bool = False,
    trailing: bytes = b"",
    scan: bytes = b"\x12\x34\x56\x78",
) -> bytes:
    """Un JPEG sintético con el ``SOF0`` de ``width`` x ``height`` (ver el módulo)."""
    parts = [b"\xff\xd8"]
    if app0 is not None:
        parts.append(app0)
    if marked:
        parts.append(_segment(0xFE, MARK))
    if exif:
        parts.append(_EXIF)
    frame = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    parts.append(_segment(0xC0, frame))
    parts.append(_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + scan)
    parts.append(b"\xff\xd9")
    return b"".join(parts) + trailing


JFIF16: Final = _JFIF16
JFXX: Final = _JFXX


def image(data: bytes, **declared: Any) -> dict[str, Any]:
    """El objeto ``image`` de ``data``; ``declared`` cambia lo que se declara."""
    document: dict[str, Any] = {
        "content_type": "image/jpeg",
        "width": 320,
        "height": 240,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "anonymized": True,
        "blur_params_version": BLUR_VERSION,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
    document.update(declared)
    return document


def submission(
    *,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    node_id: uuid.UUID,
    standard_id: uuid.UUID,
    camera_id: uuid.UUID,
    started: dt.datetime,
    standard_version: int = 1,
    photo: dict[str, Any] | None = None,
    absent_reason: str | None = None,
    synchronized: bool = True,
    offset_ms: int = 12,
    photo_id: uuid.UUID | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Una ``EpisodePhotoSubmission`` de la zona: con ``photo`` (por defecto, la de ``jpeg()``) o,
    con ``absent_reason``, sin imagen y con su motivo."""
    ended = started + dt.timedelta(seconds=4)
    document: dict[str, Any] = {
        "photo_id": str(photo_id or uuid7_at(started, uuid.uuid4().bytes)),
        "contract_version": VERSION,
        "organization_id": str(organization_id),
        "plant_id": str(plant_id),
        "zone_id": str(zone_id),
        "node_id": str(node_id),
        "episode": {
            "episode_id": str(uuid7_at(started, uuid.uuid4().bytes)),
            "segment_index": 0,
            "continues": False,
            "max_segment_ms": 60_000,
        },
        "zone_mode": "commissioning",
        "classified_as": "finding",
        "family": "coexistence",
        "standard": {"standard_id": str(standard_id), "version": standard_version},
        "max_confidence": 0.91,
        "node_time": {
            "started_at": format_timestamp(started),
            "ended_at": format_timestamp(ended),
            "clock": {"synchronized": synchronized, "offset_ms": offset_ms, "source": "ntp.local"},
        },
        "episode_duration_ms": 4_000,
        "model_version": MODEL_VERSION,
        "software_version": SOFTWARE_VERSION,
    }
    if absent_reason is None:
        document["camera_id"] = str(camera_id)
        document["frame_at"] = format_timestamp(started + dt.timedelta(seconds=1))
        document["image"] = photo if photo is not None else image(jpeg())
    else:
        document["image_absent_reason"] = absent_reason
    if validate:
        api.parse_episode_photo_submission(json.dumps(document).encode())
    return document
