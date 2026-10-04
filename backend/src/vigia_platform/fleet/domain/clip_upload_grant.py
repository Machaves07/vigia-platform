"""``ClipUploadGrant``: concesión de subida de un clip del nodo (LC-GOB-13; DE §3.13 y notas).

``domain-entities.md`` de U-03 §3.13 con sus notas del 2026-09-23 (``purpose``), BR-GOB-93 y 94,
PAT-GOB-SEG-03 y las entradas A-05 y A-29 de la adenda:

- clave ``org/{organization_id}/plant/{plant_id}/zone/{zone_id}/node/{node_id}/{clip_id}.{ext}``
  (≤ 512, BR-NUC-65), con ``ext`` del tipo de contenido (``mp4`` o ``jpg``);
- ``content_type ∈ video/mp4 | image/jpeg`` y ``max_size_bytes`` = el tamaño declarado (≤ 50 MB):
  el almacén solo acepta los bytes cuya suma está firmada, así que el objeto tiene ese tamaño;
- ``expires_at = issued_at + 15 min`` (marcas truncadas al milisegundo del contrato);
- ``required_headers`` (≤ 8): el tipo de contenido, ``x-amz-checksum-sha256`` (la SHA-256
  declarada en base64, A-05) y ``x-amz-meta-vigia-anonymized: 1`` (A-29);
- ``purpose ∈ evidence | verification`` (``evidence`` por defecto, nº 32).

**Repetición de la petición** (decisión del redactor, PR-GOB-20; ``repeat_outcome``): la misma
``clip_id`` con los mismos parámetros y la concesión vigente → URL nueva con el tiempo que le
queda; vencida y sin objeto → se **reemite** (misma fila, ``issued_at`` nuevo, ``gob_0021``); con
otros parámetros, con el objeto ya subido o con la concesión ya cerrada → conflicto.

**Huérfanos** (BR-GOB-94 y sus notas; ``orphan_outcome``): una concesión ``evidence`` emitida
hace más de 24 h que sigue ``issued`` (ningún registro aceptado la citó, TASK-221) pasa a
``orphan`` si su objeto está en el almacén y a ``expired`` si no lo está; una ``verification``
nunca. **Lectura del redactor** (Notes de TASK-222): la plataforma no recibe aviso del ``PUT``, así
que «usada» se conoce al citarla un registro, al confirmarla (verificación) o por la consulta de
metadatos de ``mark_orphan_clips``; en este último caso la concesión pasa ``issued → used →
orphan`` en la misma transacción (``used_at = orphaned_at``), que es lo que admite la guarda de
``gob_0018``.

Dominio puro: sin base, sin almacén y sin hora del sistema.
"""

from __future__ import annotations

import base64
import binascii
import enum
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from vigia_contracts.models.enumerations import ClipUploadPurpose, MediaKind

from vigia_platform.fleet.domain.enums import UploadGrantStatus

__all__ = [
    "ANONYMIZED_HEADER",
    "ANONYMIZED_VALUE",
    "CHECKSUM_HEADER",
    "CLIP_GRANT_TTL",
    "CLIP_MAX_BYTES",
    "CONTENT_TYPE_HEADER",
    "ORPHAN_AFTER",
    "STORAGE_KEY_MAX_LENGTH",
    "ClipContentType",
    "ClipGrantRequestInvalid",
    "ClipUploadGrant",
    "OrphanOutcome",
    "RepeatOutcome",
    "clip_storage_key",
    "orphan_outcome",
    "repeat_outcome",
    "sha256_from_headers",
    "to_millisecond",
]

CLIP_GRANT_TTL: Final = timedelta(minutes=15)
"""Vigencia de la concesión y de su URL (``expires_at = issued_at + 15 min``, BR-GOB-93)."""

CLIP_MAX_BYTES: Final = 52_428_800
"""Tope de un clip: 50 MB (BR-CTR-06; restricción ``clip_upload_grant_max_size``)."""

ORPHAN_AFTER: Final = timedelta(hours=24)
"""Un clip subido sin registro que lo cite en 24 h pasa a ``orphan`` (BR-GOB-94)."""

STORAGE_KEY_MAX_LENGTH: Final = 512

CHECKSUM_HEADER: Final = "x-amz-checksum-sha256"
ANONYMIZED_HEADER: Final = "x-amz-meta-vigia-anonymized"
CONTENT_TYPE_HEADER: Final = "content-type"
ANONYMIZED_VALUE: Final = "1"
"""Marca de anonimización del objeto (A-29: ``1``, no ``true``)."""

_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


class ClipGrantRequestInvalid(ValueError):
    """Petición fuera de los límites de la concesión: ``schema_invalid`` con ``field``."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"concesión de clip no válida: {detail}")
        self.field = field


class ClipContentType(enum.StrEnum):
    """Tipos admitidos de un clip (DE §3.13; BR-CTR-06)."""

    MP4 = "video/mp4"
    JPEG = "image/jpeg"

    @property
    def extension(self) -> str:
        """``ext`` de la clave del objeto."""
        return "mp4" if self is ClipContentType.MP4 else "jpg"

    @property
    def media_kind(self) -> MediaKind:
        return MediaKind.VIDEO if self is ClipContentType.MP4 else MediaKind.IMAGE


class RepeatOutcome(enum.Enum):
    """Qué hacer con una petición de concesión de un ``clip_id`` que ya tiene concesión."""

    RENEW_URL = "renew_url"
    """Misma petición, vigente y sin objeto: URL nueva con el tiempo que le queda."""
    REISSUE = "reissue"
    """Misma petición, vencida y sin objeto: la misma fila con ``issued_at`` nuevo."""
    CONFLICT = "conflict"
    """Otra petición, objeto ya subido o concesión cerrada: nunca un segundo ``PUT``."""


class OrphanOutcome(enum.Enum):
    """Resultado del barrido de ``mark_orphan_clips`` sobre una concesión."""

    KEEP = "keep"
    ORPHAN = "orphan"
    EXPIRE = "expire"


def to_millisecond(moment: datetime) -> datetime:
    """``moment`` truncado al milisegundo (las marcas del contrato llevan milisegundos)."""
    if moment.utcoffset() is None:
        raise ValueError("la marca de tiempo debe llevar zona horaria")
    return moment.replace(microsecond=moment.microsecond - moment.microsecond % 1000)


def _uuid(value: object, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ClipGrantRequestInvalid(field, f"{field} debe ser un UUID")
    return uuid.UUID(int=value.int)


def clip_storage_key(
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    node_id: uuid.UUID,
    clip_id: uuid.UUID,
    content_type: ClipContentType,
) -> str:
    """``org/{o}/plant/{p}/zone/{z}/node/{n}/{clip_id}.{ext}`` (BR-NUC-65; ≤ 512)."""
    key = (
        f"org/{_uuid(organization_id, 'organization_id')}/plant/{_uuid(plant_id, 'plant_id')}"
        f"/zone/{_uuid(zone_id, 'zone_id')}/node/{_uuid(node_id, 'node_id')}"
        f"/{_uuid(clip_id, 'clip_id')}.{ClipContentType(content_type).extension}"
    )
    if len(key) > STORAGE_KEY_MAX_LENGTH:  # pragma: no cover - 5 UUID: 208 caracteres
        raise ClipGrantRequestInvalid("clip_id", "la clave supera 512 caracteres")
    return key


def _checksum_b64(sha256_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")


def sha256_from_headers(headers: Mapping[str, str]) -> str:
    """La SHA-256 hexadecimal de ``x-amz-checksum-sha256`` de una concesión guardada."""
    try:
        digest = base64.b64decode(headers[CHECKSUM_HEADER], validate=True)
    except (KeyError, binascii.Error, ValueError, TypeError):
        raise ValueError("required_headers sin una suma SHA-256 válida") from None
    if len(digest) != 32:
        raise ValueError("required_headers sin una suma SHA-256 válida")
    return digest.hex()


@dataclass(frozen=True, slots=True)
class ClipUploadGrant:
    """§3.13 ``ClipUploadGrant`` 🔒 (tabla ``fleet.clip_upload_grant``)."""

    clip_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    purpose: ClipUploadPurpose
    content_type: ClipContentType
    sha256: str
    """SHA-256 declarada en la petición, hexadecimal en minúsculas."""
    max_size_bytes: int
    storage_key: str
    issued_at: datetime
    expires_at: datetime
    status: UploadGrantStatus = UploadGrantStatus.ISSUED
    used_at: datetime | None = None
    orphaned_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        clip_id: uuid.UUID,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        purpose: ClipUploadPurpose | None,
        media_kind: MediaKind,
        content_type: str,
        sha256: str,
        size_bytes: int,
        issued_at: datetime,
    ) -> ClipUploadGrant:
        """Concesión nueva en ``issued``; ``ClipGrantRequestInvalid`` fuera de los límites.

        Los campos ya pasaron el lector estricto de U-01; aquí se comprueba lo que el esquema no
        expresa (el tipo de contenido concuerda con el tipo de medio) y se fijan los límites.
        """
        try:
            checked_type = ClipContentType(content_type)
        except ValueError:
            raise ClipGrantRequestInvalid(
                "content_type", "tipo de contenido fuera de la lista"
            ) from None
        if MediaKind(media_kind) is not checked_type.media_kind:
            raise ClipGrantRequestInvalid("content_type", "el tipo no concuerda con media_kind")
        if not isinstance(sha256, str) or _SHA256_HEX.fullmatch(sha256) is None:
            raise ClipGrantRequestInvalid("sha256", "sha256 hexadecimal de 64 caracteres")
        if type(size_bytes) is not int or not 1 <= size_bytes <= CLIP_MAX_BYTES:
            raise ClipGrantRequestInvalid("size_bytes", f"tamaño entre 1 y {CLIP_MAX_BYTES}")
        issued = to_millisecond(issued_at)
        return cls(
            clip_id=_uuid(clip_id, "clip_id"),
            organization_id=_uuid(organization_id, "organization_id"),
            plant_id=_uuid(plant_id, "plant_id"),
            zone_id=_uuid(zone_id, "zone_id"),
            node_id=_uuid(node_id, "node_id"),
            purpose=ClipUploadPurpose(purpose or ClipUploadPurpose.EVIDENCE),
            content_type=checked_type,
            sha256=sha256,
            max_size_bytes=size_bytes,
            storage_key=clip_storage_key(
                organization_id, plant_id, zone_id, node_id, clip_id, checked_type
            ),
            issued_at=issued,
            expires_at=issued + CLIP_GRANT_TTL,
        )

    @property
    def required_headers(self) -> dict[str, str]:
        """Las cabeceras que el nodo envía **exactamente** en el ``PUT`` (A-05, A-29)."""
        return {
            CONTENT_TYPE_HEADER: self.content_type.value,
            CHECKSUM_HEADER: _checksum_b64(self.sha256),
            ANONYMIZED_HEADER: ANONYMIZED_VALUE,
        }

    @property
    def metadata_headers(self) -> dict[str, str]:
        """Los metadatos de usuario firmados en la URL (``StoragePort.presign_put``)."""
        return {ANONYMIZED_HEADER: ANONYMIZED_VALUE}

    def same_request(self, other: ClipUploadGrant) -> bool:
        """¿Pide ``other`` exactamente lo que esta concesión concedió (mismo nodo y zona)?"""
        return (
            self.clip_id == other.clip_id
            and self.organization_id == other.organization_id
            and self.plant_id == other.plant_id
            and self.zone_id == other.zone_id
            and self.node_id == other.node_id
            and self.purpose is other.purpose
            and self.content_type is other.content_type
            and self.sha256 == other.sha256
            and self.max_size_bytes == other.max_size_bytes
            and self.storage_key == other.storage_key
        )

    def remaining(self, now: datetime) -> timedelta:
        """Vigencia que le queda en ``now`` (nunca negativa)."""
        return max(self.expires_at - now, timedelta(0))

    def reissued(self, now: datetime) -> ClipUploadGrant:
        """La misma concesión con ``issued_at`` nuevo: solo ``issued`` y ya vencida."""
        issued = to_millisecond(now)
        if self.status is not UploadGrantStatus.ISSUED or issued < self.expires_at:
            raise ValueError("solo se reemite una concesión emitida y vencida")
        return replace(self, issued_at=issued, expires_at=issued + CLIP_GRANT_TTL)


def repeat_outcome(
    existing: ClipUploadGrant, requested: ClipUploadGrant, now: datetime, *, uploaded: bool
) -> RepeatOutcome:
    """Qué responde una petición repetida de ``existing.clip_id`` (PR-GOB-20)."""
    if (
        not existing.same_request(requested)
        or uploaded
        or existing.status is not UploadGrantStatus.ISSUED
    ):
        return RepeatOutcome.CONFLICT
    if now < existing.expires_at:
        return RepeatOutcome.RENEW_URL
    return RepeatOutcome.REISSUE


def orphan_outcome(grant: ClipUploadGrant, now: datetime, *, uploaded: bool) -> OrphanOutcome:
    """Qué hace ``mark_orphan_clips`` con ``grant`` en ``now`` (BR-GOB-94 y sus notas)."""
    if (
        grant.purpose is not ClipUploadPurpose.EVIDENCE
        or grant.status is not UploadGrantStatus.ISSUED
        or now < grant.issued_at + ORPHAN_AFTER
    ):
        return OrphanOutcome.KEEP
    if uploaded:
        return OrphanOutcome.ORPHAN
    return OrphanOutcome.EXPIRE if now >= grant.expires_at else OrphanOutcome.KEEP
