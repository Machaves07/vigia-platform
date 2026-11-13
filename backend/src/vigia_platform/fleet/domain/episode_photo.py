"""Foto del episodio de la sombra: verificaciones puras (TASK-572; A-112; BR-GOB-84; NFR-GOB-33).

El nodo en ``commissioning`` presenta **siempre** un registro ``episode_photo`` por tramo
clasificado (v1.5.0 del contrato): con la foto difuminada del cuadro de mayor confianza en línea o
con el motivo cerrado de su ausencia. Es material de puesta en marcha: no entra en el expediente ni
en la cadena (QF10).

**Orden** (BR-GOB-84; el primer fallo decide el código, el de menor índice):
(1) versión, (2) certificado y zona del nodo **antes** que el esquema, (3) tamaño (307 200 bytes),
(4) esquema, (5) **idempotencia**, (6) imagen, (7) catálogo y estándar, (8) compuerta de
**montaje**, (9) escritura. La idempotencia va antes que la imagen, el catálogo y la compuerta: un
reenvío idéntico es ``accepted_duplicate`` con el recibo original aunque después hayan cambiado la
compuerta o el catálogo.

**Imagen** (paso 6; el mismo orden y los mismos ``field`` que la plataforma simulada del contrato
v1.5.0 y ``ingest.yaml``, precisiones del 12-11 de A-112): el ``data_base64`` decodificado mide
``size_bytes`` (``clip_hash_mismatch`` en ``image.size_bytes``) y tiene la suma ``sha256``
(``image.sha256``); ``vigia_contracts.images.jpeg_facts`` lo lee como JPEG: un ``APP1`` a
``APP15``, un ``APP0`` que no es el ``JFIF`` de 16 bytes sin miniatura, más de un ``APP0`` o bytes
tras ``EOI`` son ``clip_not_anonymized`` y cualquier otro defecto ``clip_hash_mismatch`` (los dos
en ``image.data_base64``); ancho y alto del ``SOF`` iguales a los declarados (``clip_hash_mismatch``
en ``image.width`` o ``image.height``, precisión del 11-11/12-11 de A-112) y el comentario
``vigia_anonymized=1`` (``clip_not_anonymized`` en ``image.data_base64``). **No** se miran los
píxeles: la verificación del difuminado es la del dueño, a ojo (A-112, punto 2).

**Catálogo** (paso 7): el estándar citado existe con esa versión en alguna versión del catálogo
vigente en la ventana del tramo (``schema_invalid`` en ``standard``) y la cámara del cuadro, si la
hay, está declarada en una de las versiones que lo citan (``schema_invalid`` en ``camera_id``).

**Vencimiento**: ``expires_at = received_at + VIGIA_EPISODE_PHOTO_RETENTION_DAYS`` (30 por
defecto, de 1 a 90; QF5), con el reloj inyectado.

Dominio puro: sin base ni reloj.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_contracts.images import JpegDefect, JpegDefectCode, jpeg_facts
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.ingest_order import CatalogVersionView

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "MAX_IMAGE_BYTES",
    "MAX_RETENTION_DAYS",
    "MIN_RETENTION_DAYS",
    "STEP_CODES",
    "EpisodePhotoRow",
    "EpisodePhotoStep",
    "ImageViolation",
    "catalog_violation",
    "check_retention_days",
    "decode_image",
    "expires_at",
    "image_violation",
    "photo_row",
]

MIN_RETENTION_DAYS: Final = 1
MAX_RETENTION_DAYS: Final = 90
DEFAULT_RETENTION_DAYS: Final = 30
"""``VIGIA_EPISODE_PHOTO_RETENTION_DAYS`` (QF5): días que vive la foto en la plataforma."""

MAX_IMAGE_BYTES: Final = 204_800
"""Tamaño máximo del JPEG (A-112, punto 2)."""

IMAGE_FIELD: Final = "image.data_base64"

_NOT_ANONYMIZED: Final = frozenset(
    {JpegDefectCode.FORBIDDEN_APP_SEGMENT, JpegDefectCode.TRAILING_DATA}
)
"""Defectos de ``jpeg_facts`` que son falta de anonimización (metadatos, miniaturas, cola)."""


class EpisodePhotoStep(enum.IntEnum):
    """Los nueve pasos de BR-GOB-84 para la foto del episodio, en su orden."""

    VERSION = 1
    CERTIFICATE = 2
    SIZE = 3
    SCHEMA = 4
    IDEMPOTENCY = 5
    IMAGE = 6
    CATALOG = 7
    GATE = 8
    WRITE = 9


STEP_CODES: Final[Mapping[EpisodePhotoStep, frozenset[RejectionCode]]] = {
    EpisodePhotoStep.VERSION: frozenset(
        {
            RejectionCode.CONTRACT_VERSION_UNSUPPORTED,
            RejectionCode.CONTRACT_VERSION_RETIRED,
            RejectionCode.SCHEMA_INVALID,
        }
    ),
    EpisodePhotoStep.CERTIFICATE: frozenset(
        {
            RejectionCode.NODE_NOT_ENROLLED,
            RejectionCode.NODE_REVOKED,
            RejectionCode.NODE_ZONE_MISMATCH,
        }
    ),
    EpisodePhotoStep.SIZE: frozenset({RejectionCode.PAYLOAD_TOO_LARGE}),
    EpisodePhotoStep.SCHEMA: frozenset({RejectionCode.SCHEMA_INVALID}),
    EpisodePhotoStep.IDEMPOTENCY: frozenset({RejectionCode.IDEMPOTENCY_CONFLICT}),
    EpisodePhotoStep.IMAGE: frozenset(
        {RejectionCode.CLIP_HASH_MISMATCH, RejectionCode.CLIP_NOT_ANONYMIZED}
    ),
    EpisodePhotoStep.CATALOG: frozenset(
        {RejectionCode.SCHEMA_INVALID, RejectionCode.TIMESTAMP_OUT_OF_WINDOW}
    ),
    EpisodePhotoStep.GATE: frozenset({RejectionCode.ZONE_GATE_NOT_APPROVED}),
    EpisodePhotoStep.WRITE: frozenset(
        {RejectionCode.TEMPORARILY_UNAVAILABLE, RejectionCode.STORAGE_UNAVAILABLE}
    ),
}
"""Los códigos que puede dar cada paso."""


# --- Vencimiento ---------------------------------------------------------------------------------


def check_retention_days(days: object) -> int:
    """``days`` si es un entero de 1 a 90; ``ValueError`` si no (el proceso no arranca)."""
    if type(days) is not int or not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        raise ValueError("VIGIA_EPISODE_PHOTO_RETENTION_DAYS va de 1 a 90 días")
    return days


def expires_at(received_at: datetime, days: int) -> datetime:
    """El vencimiento de una foto recibida en ``received_at`` (exactamente ``days`` después)."""
    if received_at.utcoffset() is None:
        raise ValueError("received_at lleva zona horaria")
    return received_at + timedelta(days=check_retention_days(days))


# --- Paso 6: la imagen en línea ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageViolation:
    """El primer fallo de la imagen: su código y su ``field``."""

    code: RejectionCode
    field: str


def decode_image(image: Mapping[str, Any]) -> bytes:
    """Los bytes del JPEG; ``ValueError`` si ``data_base64`` no es base64 estándar.

    El esquema ya fija el alfabeto y el relleno; esto es la segunda barrera del paso 4.
    """
    try:
        return base64.b64decode(str(image["data_base64"]), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data_base64 no es base64 estándar") from None


def image_violation(image: Mapping[str, Any], data: bytes) -> ImageViolation | None:
    """El primer fallo de la imagen declarada ``image`` con los bytes ``data`` (o nada)."""
    if len(data) != image["size_bytes"]:
        return ImageViolation(RejectionCode.CLIP_HASH_MISMATCH, "image.size_bytes")
    if hashlib.sha256(data).hexdigest() != str(image["sha256"]):
        return ImageViolation(RejectionCode.CLIP_HASH_MISMATCH, "image.sha256")
    facts = jpeg_facts(data)
    if isinstance(facts, JpegDefect):
        if facts.code in _NOT_ANONYMIZED:
            return ImageViolation(RejectionCode.CLIP_NOT_ANONYMIZED, IMAGE_FIELD)
        return ImageViolation(RejectionCode.CLIP_HASH_MISMATCH, IMAGE_FIELD)
    if facts.width != image["width"]:
        return ImageViolation(RejectionCode.CLIP_HASH_MISMATCH, "image.width")
    if facts.height != image["height"]:
        return ImageViolation(RejectionCode.CLIP_HASH_MISMATCH, "image.height")
    if not facts.marked:
        return ImageViolation(RejectionCode.CLIP_NOT_ANONYMIZED, IMAGE_FIELD)
    return None


# --- Paso 7: catálogo y estándar -----------------------------------------------------------------


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def catalog_violation(
    versions: Sequence[CatalogVersionView], document: Mapping[str, Any], window: Window
) -> str | None:
    """``field`` del ``schema_invalid`` del paso 7, o ``None``.

    ``standard`` si ninguna versión vigente en ``window`` cita el estándar con su versión;
    ``camera_id`` si la cámara del cuadro no está en ninguna de las que lo citan.
    """
    standard = document["standard"]
    standard_id, version = _uuid(standard["standard_id"]), standard["version"]
    citing = [
        item
        for item in versions
        if item.in_force_during(window)
        and standard_id is not None
        and type(version) is int
        and item.cites(standard_id, version)
    ]
    if not citing:
        return "standard"
    camera = document.get("camera_id")
    if camera is not None and not any(_uuid(camera) in item.cameras for item in citing):
        return "camera_id"
    return None


# --- Paso 9: la fila ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpisodePhotoRow:
    """Una fila de ``fleet.episode_photo`` (``gob_0042``)."""

    organization_id: uuid.UUID
    photo_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    episode_id: uuid.UUID
    segment_index: int
    classified_as: str
    family: str
    standard_id: uuid.UUID
    standard_version: int
    max_confidence: float
    started_at: datetime
    ended_at: datetime
    received_at: datetime
    expires_at: datetime
    camera_id: uuid.UUID | None
    frame_at: datetime | None
    width: int | None
    height: int | None
    size_bytes: int | None
    sha256: str | None
    blur_params_version: str | None
    image: bytes | None
    image_absent_reason: str | None
    canonical_sha256: str
    platform_record_id: uuid.UUID

    def __repr__(self) -> str:
        # Nunca los bytes de la imagen (NFR-GOB-25).
        return f"EpisodePhotoRow(photo_id={self.photo_id}, zone_id={self.zone_id})"


def _instant(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def photo_row(
    submission: Mapping[str, Any],
    *,
    data: bytes | None,
    received_at: datetime,
    retention_days: int,
    canonical_sha256: str,
    platform_record_id: uuid.UUID,
) -> EpisodePhotoRow:
    """La fila de la presentación ``submission`` ya validada con el modelo estricto."""
    image = submission.get("image")
    camera, frame = submission.get("camera_id"), submission.get("frame_at")
    standard = submission["standard"]
    episode = submission["episode"]
    node_time = submission["node_time"]
    return EpisodePhotoRow(
        organization_id=uuid.UUID(str(submission["organization_id"])),
        photo_id=uuid.UUID(str(submission["photo_id"])),
        plant_id=uuid.UUID(str(submission["plant_id"])),
        zone_id=uuid.UUID(str(submission["zone_id"])),
        node_id=uuid.UUID(str(submission["node_id"])),
        episode_id=uuid.UUID(str(episode["episode_id"])),
        segment_index=int(episode["segment_index"]),
        classified_as=str(submission["classified_as"]),
        family=str(submission["family"]),
        standard_id=uuid.UUID(str(standard["standard_id"])),
        standard_version=int(standard["version"]),
        max_confidence=float(submission["max_confidence"]),
        started_at=_instant(node_time["started_at"]),
        ended_at=_instant(node_time["ended_at"]),
        received_at=received_at,
        expires_at=expires_at(received_at, retention_days),
        camera_id=None if camera is None else uuid.UUID(str(camera)),
        frame_at=None if frame is None else _instant(frame),
        width=None if image is None else int(image["width"]),
        height=None if image is None else int(image["height"]),
        size_bytes=None if image is None else int(image["size_bytes"]),
        sha256=None if image is None else str(image["sha256"]),
        blur_params_version=None if image is None else str(image["blur_params_version"]),
        image=data,
        image_absent_reason=submission.get("image_absent_reason"),
        canonical_sha256=canonical_sha256,
        platform_record_id=platform_record_id,
    )
