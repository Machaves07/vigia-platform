"""Verificación de adjuntos en el orden fijo de PAT-LAZ-SEG-05.

El lector recibe únicamente el encabezado acotado del objeto: no materializa el adjunto entero.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass

from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.shared.storage import ObjectHead, StoragePort

__all__ = [
    "AttachmentCandidate",
    "AttachmentRejected",
    "AttachmentRejection",
    "ClosureAttachmentVerifier",
    "verify_attachment",
]


class AttachmentRejection(enum.StrEnum):
    TYPE = "attachment_type_not_allowed"
    METADATA = "attachment_metadata_not_allowed"
    SIZE = "attachment_too_large"
    HASH = "attachment_hash_mismatch"
    DECLARATION = "attachment_declaration_missing"


class AttachmentRejected(ValueError):
    """Rechazo cerrado; el llamador no debe persistir nada."""

    def __init__(self, reason: AttachmentRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


_MAGIC: Mapping[AttachmentContentType, tuple[bytes, ...]] = {
    AttachmentContentType.IMAGE_JPEG: (b"\xff\xd8\xff",),
    AttachmentContentType.IMAGE_PNG: (b"\x89PNG\r\n\x1a\n",),
    AttachmentContentType.APPLICATION_PDF: (b"%PDF-",),
}
_GPS_MARKERS = (b"GPSLatitude", b"GPSLongitude", b"GPSInfo")


@dataclass(frozen=True, slots=True)
class AttachmentCandidate:
    """Declaración y metadatos ya leídos para una única verificación."""

    content_type: object
    size_bytes: object
    sha256: object
    declared_no_identifiable_person: object
    head: ObjectHead | None
    header: bytes


@dataclass(frozen=True, slots=True)
class ClosureAttachmentVerifier:
    """Obtiene solo metadatos y encabezado acotado antes de aplicar las guardas."""

    storage: StoragePort

    async def verify(
        self,
        *,
        storage_key: str,
        content_type: object,
        size_bytes: object,
        sha256: object,
        declared_no_identifiable_person: object,
    ) -> None:
        head = await self.storage.head_object(storage_key)
        header = b"" if head is None else await self.storage.get_object_range(storage_key, 65_535)
        verify_attachment(
            AttachmentCandidate(
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                declared_no_identifiable_person=declared_no_identifiable_person,
                head=head,
                header=header,
            )
        )


def verify_attachment(candidate: AttachmentCandidate) -> None:
    """Aplica cada guarda exactamente una vez y en el orden del diseño."""
    # 1. Esquema: los valores que no respetan la forma se tratan como tipo no permitido.
    if not isinstance(candidate.content_type, str):
        raise AttachmentRejected(AttachmentRejection.TYPE)
    try:
        content_type = AttachmentContentType(candidate.content_type)
    except ValueError:
        raise AttachmentRejected(AttachmentRejection.TYPE) from None
    # 2. Tipo declarado.
    if candidate.head is None or candidate.head.content_type != content_type.value:
        raise AttachmentRejected(AttachmentRejection.TYPE)
    # 3. Firma de bytes (solo encabezado acotado).
    if not any(candidate.header.startswith(magic) for magic in _MAGIC[content_type]):
        raise AttachmentRejected(AttachmentRejection.TYPE)
    # 4. Posición: no se alteran bytes ni metadatos.
    if content_type is not AttachmentContentType.APPLICATION_PDF and any(
        marker in candidate.header for marker in _GPS_MARKERS
    ):
        raise AttachmentRejected(AttachmentRejection.METADATA)
    # 5. Tamaño.
    invalid_size = (
        type(candidate.size_bytes) is not int
        or not 1 <= candidate.size_bytes <= 10 * 1024 * 1024
        or candidate.head.size_bytes != candidate.size_bytes
    )
    if invalid_size:
        raise AttachmentRejected(AttachmentRejection.SIZE)
    # 6. Suma del objeto completo, calculada por S3.
    if (
        not isinstance(candidate.sha256, str)
        or candidate.head.full_object_sha256_hex != candidate.sha256
    ):
        raise AttachmentRejected(AttachmentRejection.HASH)
    # 7. Declaración firmada (la firma se verifica por el escritor del expediente).
    if candidate.declared_no_identifiable_person is not True:
        raise AttachmentRejected(AttachmentRejection.DECLARATION)
