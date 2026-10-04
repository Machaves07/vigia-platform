"""``DocumentUploadGrant``: concesión de subida de un documento firmado de planta (LC-GOB-05).

``domain-entities.md`` de U-03 §3.14 y sus dos notas del 2026-09-23: el documento es **de planta**
(sin zona ni nodo), pertenece al módulo ``catalog`` y vive en ``vigia-evidence`` bajo
``org/{organization_id}/plant/{plant_id}/documents/{document_id}.{ext}`` (infraestructura §4.1,
adenda A-22 y A-33). Límites de la nota del 2026-09-20 de ``business-rules.md`` §8: hasta
**20 MB**, ``application/pdf``, ``image/jpeg`` o ``image/png``, a lo sumo **10** por acta o
acuerdo y concesión de **15 minutos**.

Dominio puro: sin base, sin almacén y sin hora del sistema. El estado ``expired`` no se escribe al
vencer: se **deriva** al registrar o al consultar (``effective_status``) cuando la concesión venció
sin objeto subido. ``orphan`` es un valor de la lista cerrada ``upload_grant_status`` (compartida
con los clips), pero ninguna regla del diseño lo produce para documentos y la guarda de la base
(``gob_0017``) solo admite ``issued → used`` e ``issued → expired``: aquí no se usa.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.fleet.domain.enums import UploadGrantStatus

__all__ = [
    "DEFAULT_DOCUMENTS_PREFIX",
    "DOCUMENTS_MAX_BYTES",
    "DOCUMENT_GRANT_TTL",
    "MAX_DOCUMENT_REFS",
    "RESERVED_PREFIXES",
    "DocumentContentType",
    "DocumentRef",
    "DocumentRequestInvalid",
    "DocumentSettings",
    "DocumentUploadGrant",
    "document_storage_key",
]

DOCUMENTS_MAX_BYTES: Final = 20_971_520
"""Tope de un documento: 20 MB (NFR-GOB-23; ``VIGIA_DOCUMENTS_MAX_BYTES``; restricción
``document_upload_grant_size``)."""

DOCUMENT_GRANT_TTL: Final = timedelta(minutes=15)
"""Vigencia de la concesión y de su URL (``expires_at = issued_at + 15 min``)."""

MAX_DOCUMENT_REFS: Final = 10
"""Documentos como mucho por acta o acuerdo (nota del 2026-09-20 de BR §8)."""

DEFAULT_DOCUMENTS_PREFIX: Final = "documents/"
"""``VIGIA_DOCUMENTS_PREFIX`` (infraestructura §6)."""

RESERVED_PREFIXES: Final = frozenset({"node/", "closure/", "zone/"})
"""Segmentos de otras clases de objeto de la misma planta: clips del nodo (LC-GOB-13, bajo
``zone/{zone_id}/node/``) y adjuntos de cierre de U-04 (``closure/``). El prefijo de documentos
es disjunto de todos (infraestructura §4.1, «Disyunción de prefijos»)."""

_PREFIX: Final = re.compile(r"[a-z][a-z0-9-]{0,30}/")
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


class DocumentContentType(enum.StrEnum):
    """Tipos admitidos (nota del 2026-09-23 de §3.14: ``image/png`` además de PDF y JPEG)."""

    PDF = "application/pdf"
    JPEG = "image/jpeg"
    PNG = "image/png"

    @property
    def extension(self) -> str:
        """``ext`` de la clave del objeto (infraestructura §4.1)."""
        return _EXTENSIONS[self]


_EXTENSIONS: Final[Mapping[DocumentContentType, str]] = {
    DocumentContentType.PDF: "pdf",
    DocumentContentType.JPEG: "jpg",
    DocumentContentType.PNG: "png",
}


class DocumentRequestInvalid(ValueError):
    """Petición o referencia fuera de los límites: ``invalid_request`` sin detalle."""

    code: Final = "invalid_request"

    def __init__(self, detail: str) -> None:
        super().__init__(f"documento no válido: {detail}")


@dataclass(frozen=True, slots=True)
class DocumentSettings:
    """``VIGIA_DOCUMENTS_PREFIX`` y ``VIGIA_DOCUMENTS_MAX_BYTES`` ya validados."""

    prefix: str = DEFAULT_DOCUMENTS_PREFIX
    max_bytes: int = DOCUMENTS_MAX_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.prefix, str) or _PREFIX.fullmatch(self.prefix) is None:
            raise ValueError("el prefijo de documentos debe ser un segmento en minúsculas con /")
        if self.prefix in RESERVED_PREFIXES:
            raise ValueError("el prefijo de documentos no puede ser el de otra clase de objeto")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= DOCUMENTS_MAX_BYTES:
            raise ValueError(f"el tamaño máximo debe estar entre 1 y {DOCUMENTS_MAX_BYTES} bytes")


def _uuid(value: object, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise DocumentRequestInvalid(f"{name} debe ser un UUID")
    return uuid.UUID(int=value.int)


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise DocumentRequestInvalid("sha256 debe ser hexadecimal en minúsculas de 64 caracteres")
    return value


def _size(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise DocumentRequestInvalid(f"size_bytes debe estar entre 1 y {maximum}")
    return value


def _closed[E: enum.StrEnum](enumeration: type[E], value: object, name: str) -> E:
    if not isinstance(value, str):
        raise DocumentRequestInvalid(f"{name} fuera de la lista")
    try:
        return enumeration(value)
    except ValueError:
        raise DocumentRequestInvalid(f"{name} fuera de la lista") from None


def document_storage_key(
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    document_id: uuid.UUID,
    content_type: DocumentContentType,
    settings: DocumentSettings,
) -> str:
    """``org/{organization_id}/plant/{plant_id}/documents/{document_id}.{ext}``."""
    return (
        f"org/{_uuid(organization_id, 'organization_id')}/plant/{_uuid(plant_id, 'plant_id')}/"
        f"{settings.prefix}{_uuid(document_id, 'document_id')}.{content_type.extension}"
    )


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """``document_ref = {document_id, storage_key, sha256, content_type, size_bytes}``.

    La forma que guardan el acta de alcance, el acuerdo y la política (§2.6, §2.8, §2.10) y que
    ``verify_document_refs`` compara con la concesión.
    """

    document_id: uuid.UUID
    storage_key: str
    sha256: str
    content_type: DocumentContentType
    size_bytes: int

    @classmethod
    def parse(cls, value: object) -> DocumentRef:
        """Una referencia de una petición (JSON) o ya construida; estricta, sin coerción."""
        if isinstance(value, DocumentRef):
            return value
        if not isinstance(value, Mapping) or set(value) != _REF_FIELDS:
            raise DocumentRequestInvalid("document_ref debe tener exactamente sus cinco campos")
        raw_id = value["document_id"]
        if not isinstance(raw_id, str | uuid.UUID):
            raise DocumentRequestInvalid("document_id debe ser un UUID")
        try:
            document_id = raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(raw_id)
        except ValueError:
            raise DocumentRequestInvalid("document_id debe ser un UUID") from None
        if isinstance(raw_id, str) and str(document_id) != raw_id:
            # Sin coerción: solo la forma canónica (sin llaves, ``urn:`` ni mayúsculas).
            raise DocumentRequestInvalid("document_id debe ser un UUID canónico")
        storage_key = value["storage_key"]
        if not isinstance(storage_key, str) or not 1 <= len(storage_key) <= 512:
            raise DocumentRequestInvalid("storage_key no válida")
        return cls(
            document_id=uuid.UUID(int=document_id.int),
            storage_key=storage_key,
            sha256=_sha256(value["sha256"]),
            content_type=_closed(DocumentContentType, value["content_type"], "content_type"),
            size_bytes=_size(value["size_bytes"], DOCUMENTS_MAX_BYTES),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "storage_key": self.storage_key,
            "sha256": self.sha256,
            "content_type": self.content_type.value,
            "size_bytes": self.size_bytes,
        }


_REF_FIELDS: Final = frozenset(
    {"document_id", "storage_key", "sha256", "content_type", "size_bytes"}
)


@dataclass(frozen=True, slots=True)
class DocumentUploadGrant:
    """§3.14 ``DocumentUploadGrant`` 🔒 (tabla ``catalog.document_upload_grant``)."""

    document_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    kind: DocumentKind
    content_type: DocumentContentType
    storage_key: str
    sha256: str
    size_bytes: int
    issued_at: datetime
    expires_at: datetime
    status: UploadGrantStatus

    @classmethod
    def issue(
        cls,
        *,
        document_id: uuid.UUID,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        kind: object,
        content_type: object,
        size_bytes: object,
        sha256: object,
        issued_at: datetime,
        settings: DocumentSettings,
    ) -> DocumentUploadGrant:
        """Concesión nueva en ``issued``; ``DocumentRequestInvalid`` fuera de los límites."""
        if issued_at.utcoffset() is None:
            raise ValueError("issued_at debe llevar zona horaria")
        checked_type = _closed(DocumentContentType, content_type, "content_type")
        return cls(
            document_id=_uuid(document_id, "document_id"),
            organization_id=_uuid(organization_id, "organization_id"),
            plant_id=_uuid(plant_id, "plant_id"),
            kind=_closed(DocumentKind, kind, "kind"),
            content_type=checked_type,
            storage_key=document_storage_key(
                organization_id, plant_id, document_id, checked_type, settings
            ),
            sha256=_sha256(sha256),
            size_bytes=_size(size_bytes, settings.max_bytes),
            issued_at=issued_at,
            expires_at=issued_at + DOCUMENT_GRANT_TTL,
            status=UploadGrantStatus.ISSUED,
        )

    @property
    def ref(self) -> DocumentRef:
        return DocumentRef(
            document_id=self.document_id,
            storage_key=self.storage_key,
            sha256=self.sha256,
            content_type=self.content_type,
            size_bytes=self.size_bytes,
        )

    def matches(self, ref: DocumentRef) -> bool:
        """¿La referencia que trae el registro es exactamente la concedida?"""
        return ref == self.ref

    def object_matches(
        self, *, size_bytes: int, content_type: str | None, sha256_hex: str | None
    ) -> bool:
        """¿Los metadatos del objeto (``head_object``) son los concedidos? Los tres, siempre.

        ``sha256_hex`` es la SHA-256 del objeto **entero** que guardó el almacén
        (``ObjectHead.full_object_sha256_hex``); sin ella (subida sin suma o compuesta), no.
        """
        return (
            size_bytes == self.size_bytes
            and content_type == self.content_type.value
            and sha256_hex is not None
            and sha256_hex == self.sha256
        )

    def effective_status(self, now: datetime, *, uploaded: bool) -> UploadGrantStatus:
        """El estado al registrar o al consultar: ``expired`` si venció sin objeto subido."""
        if self.status is UploadGrantStatus.ISSUED and now >= self.expires_at and not uploaded:
            return UploadGrantStatus.EXPIRED
        return self.status
