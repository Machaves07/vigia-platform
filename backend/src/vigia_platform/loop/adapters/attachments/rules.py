"""Reglas comunes de los adjuntos de cierre: clave, topes y rechazos (BR-LAZ-43 y 44, §6.3).

La clave de almacenamiento se **construye** siempre desde la organización, la planta y la zona
del registro (nunca se acepta del cliente):

``org/{organization_id}/plant/{plant_id}/zone/{zone_id}/closure/{subject_id}/{attachment_id}.{ext}``

Es disjunta de ``node/`` (clips de U-02) y de ``documents/`` (U-03): ningún segmento libre
permite llegar a otro prefijo (PR-LAZ-63).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Final

from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.loop.record_types import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_ENTRY,
    MAX_ATTACHMENTS_PER_FINDING,
)

__all__ = [
    "CLOSURE_KEY_PATTERN",
    "EXTENSIONS",
    "MAX_ATTACHMENTS_PER_ENTRY",
    "MAX_ATTACHMENTS_PER_FINDING",
    "MAX_ATTACHMENT_BYTES",
    "AttachmentRejected",
    "AttachmentSchemaInvalid",
    "attachment_storage_key",
]

EXTENSIONS: Final[Mapping[AttachmentContentType, str]] = {
    AttachmentContentType.IMAGE_JPEG: "jpg",
    AttachmentContentType.IMAGE_PNG: "png",
    AttachmentContentType.APPLICATION_PDF: "pdf",
}

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CLOSURE_KEY_PATTERN: Final = re.compile(
    rf"org/(?P<organization_id>{_UUID})/plant/(?P<plant_id>{_UUID})/zone/(?P<zone_id>{_UUID})"
    rf"/closure/(?P<subject_id>{_UUID})/(?P<attachment_id>{_UUID})\.(?:jpg|png|pdf)"
)
"""Patrón cerrado de la clave de un adjunto (BR-LAZ-44); se compara con ``fullmatch``."""


class AttachmentSchemaInvalid(ValueError):
    """Paso 1 de PAT-LAZ-SEG-05: la forma no es la del esquema; 400 sin código de negocio."""


class AttachmentRejected(ValueError):
    """Rechazo de negocio de un adjunto, con su ``detail_code``; no se escribe nada."""

    def __init__(self, detail: LoopDetailCode) -> None:
        super().__init__(detail.value)
        self.detail = detail


def attachment_storage_key(
    *,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    subject_id: uuid.UUID,
    attachment_id: uuid.UUID,
    content_type: AttachmentContentType,
) -> str:
    """La clave de BR-LAZ-44; solo admite ``uuid.UUID`` y un tipo de la lista cerrada."""
    for value in (organization_id, plant_id, zone_id, subject_id, attachment_id):
        if not isinstance(value, uuid.UUID):
            raise TypeError("los identificadores de la clave deben ser uuid.UUID")
    key = (
        f"org/{organization_id}/plant/{plant_id}/zone/{zone_id}/closure/"
        f"{subject_id}/{attachment_id}.{EXTENSIONS[AttachmentContentType(content_type)]}"
    )
    if CLOSURE_KEY_PATTERN.fullmatch(key) is None:  # pragma: no cover - defensa en profundidad
        raise ValueError("clave de adjunto fuera del patrón")
    return key
