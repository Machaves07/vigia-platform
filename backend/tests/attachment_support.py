"""Apoyo de las pruebas de adjuntos de cierre (TASK-310): bytes sintéticos y dobles en memoria.

Solo datos generados: imágenes JPEG y PNG mínimas construidas byte a byte (con o sin EXIF, con
o sin IFD de GPS, con XMP o texto de PNG) y PDF de una línea. Nada sale de fotos reales.

Los dobles registran cada llamada para comprobar que ninguna operación **escribe** ni **lista**
(PR-LAZ-60, PAT-LAZ-SEG-05): el almacén en memoria solo ofrece lo que ``ClosureObjectStorage``
declara y falla con ``StorageUnavailable`` cuando se marca caído.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vigia_platform.identity.authz.authorize import Authorizer, Resource
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.adapters.attachments.repository import ActionPlace, StoredAttachment
from vigia_platform.loop.application.attachments import (
    ClosureAttachmentService,
    build_attachment_service,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.ratelimit import CLOSURE_ATTACHMENT_BUDGET, Budget
from vigia_platform.shared.storage import (
    ChecksumType,
    ObjectHead,
    PresignedRequest,
    StorageUnavailable,
)

# --- Bytes sintéticos -----------------------------------------------------------------------------


def tiff(*, gps_tags: Sequence[int] | None, big_endian: bool = False) -> bytes:
    """Estructura TIFF de EXIF: IFD0 con orientación y, si ``gps_tags``, un IFD de GPS."""
    order = ">" if big_endian else "<"
    header = (b"MM" if big_endian else b"II") + struct.pack(order + "HI", 42, 8)
    entries = [(0x0112, 3, 1, 1)]  # orientación
    if gps_tags is not None:
        entries.append((0x8825, 4, 1, 0))  # el desplazamiento se fija abajo
    ifd0_size = 2 + 12 * len(entries) + 4
    gps_offset = 8 + ifd0_size
    body = struct.pack(order + "H", len(entries))
    for tag, kind, count, value in entries:
        if tag == 0x8825:
            value = gps_offset
        body += struct.pack(order + "HHII", tag, kind, count, value)
    body += struct.pack(order + "I", 0)
    gps = b""
    if gps_tags is not None:
        gps = struct.pack(order + "H", len(gps_tags))
        for tag in gps_tags:
            gps += struct.pack(order + "HHII", tag, 1, 4, 0)
        gps += struct.pack(order + "I", 0)
    return header + body + gps


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def jpeg(
    *,
    exif: bytes | None = None,
    xmp: bytes | None = None,
    scan: bytes = b"\x12\x34" * 32,
    trailer: bytes = b"",
) -> bytes:
    """JPEG mínimo: SOI, APP0, APP1 opcionales, SOS con datos y EOI (más ``trailer``)."""
    data = b"\xff\xd8" + _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    if exif is not None:
        data += _segment(0xE1, b"Exif\x00\x00" + exif)
    if xmp is not None:
        data += _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + xmp)
    data += _segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + scan + b"\xff\xd9"
    return data + trailer


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def png(
    *,
    exif: bytes | None = None,
    texts: Sequence[tuple[bytes, bytes]] = (),
    after_idat: Sequence[tuple[bytes, bytes]] = (),
    idat: bytes = b"\x78\x9c\x63\x60\x00\x00\x00\x02\x00\x01",
) -> bytes:
    """PNG mínimo: IHDR, ``eXIf`` y fragmentos de texto opcionales, IDAT, más y IEND."""
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    if exif is not None:
        data += _chunk(b"eXIf", exif)
    for kind, body in texts:
        data += _chunk(kind, body)
    data += _chunk(b"IDAT", idat)
    for kind, body in after_idat:
        data += _chunk(kind, body)
    return data + _chunk(b"IEND", b"")


def raw_profile(kind: bytes, payload: bytes) -> bytes:
    """Texto de un perfil en hexadecimal como lo escriben ImageMagick y exiftool en PNG:
    ``\\n<tipo>\\n<longitud>\\n<hex en líneas de 72>\\n``."""
    hexed = payload.hex()
    lines = [hexed[start : start + 72] for start in range(0, len(hexed), 72)]
    return b"\n" + kind + b"\n" + f"{len(payload):8d}".encode() + b"\n" + "\n".join(lines).encode()


def pdf(note: bytes = b"documento sintetico") -> bytes:
    return b"%PDF-1.7\n% " + note + b"\n%%EOF\n"


XMP_WITH_GPS = b'<x:xmpmeta><rdf:Description exif:GPSLatitude="4,36.0N"/></x:xmpmeta>'
XMP_WITHOUT_GPS = b'<x:xmpmeta><rdf:Description xmp:CreatorTool="sintetico"/></x:xmpmeta>'


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_head(key: str, data: bytes, content_type: str, version: str = "v1") -> ObjectHead:
    """Los metadatos que daría ``HEAD`` de S3 para ``data`` subido con su suma."""
    return ObjectHead(
        key=key,
        size_bytes=len(data),
        checksum_sha256=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
        checksum_type=ChecksumType.FULL_OBJECT,
        content_type=content_type,
        metadata={},
        version_id=version,
    )


# --- Dobles ---------------------------------------------------------------------------------------


@dataclass
class MemoryStore:
    """``ClosureObjectStorage`` en memoria; registra cada llamada; ``down`` simula la caída."""

    clock: Clock
    objects: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    down: bool = False
    pause: bool = False
    """Cede el bucle antes de responder, para entrelazar llamadas concurrentes."""

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.pause:
            await asyncio.sleep(0)
        if self.down:
            raise StorageUnavailable(name)

    async def head_object(self, key: str) -> ObjectHead | None:
        await self._enter("head_object")
        found = self.objects.get(key)
        return None if found is None else object_head(key, found[0], found[1])

    async def get_object_prefix(
        self, key: str, length: int, *, version_id: str | None = None
    ) -> bytes:
        await self._enter("get_object_prefix")
        return self.objects[key][0][:length]

    async def get_object_suffix(
        self, key: str, length: int, *, version_id: str | None = None
    ) -> bytes:
        await self._enter("get_object_suffix")
        return self.objects[key][0][-length:]

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Any,
        ttl: timedelta = timedelta(minutes=15),
    ) -> PresignedRequest:
        await self._enter("presign_put")
        checksum = base64.b64encode(bytes.fromhex(checksum_sha256)).decode("ascii")
        return PresignedRequest(
            "PUT",
            f"https://store.invalid/{key}?ttl={int(ttl.total_seconds())}",
            {"content-type": content_type, "x-amz-checksum-sha256": checksum},
            self.clock.now() + ttl,
        )

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = timedelta(minutes=15), *, version_id: str | None = None
    ) -> PresignedRequest:
        await self._enter("presign_attachment_get")
        return PresignedRequest(
            "GET", f"https://store.invalid/{key}?v={version_id}", {}, self.clock.now() + ttl
        )


@dataclass
class MemoryRepository:
    actions: dict[uuid.UUID, ActionPlace] = field(default_factory=dict)
    attachments: dict[uuid.UUID, StoredAttachment] = field(default_factory=dict)
    pause: bool = False

    async def action_place(self, context: ScopeContext, action_id: uuid.UUID) -> ActionPlace | None:
        if self.pause:
            await asyncio.sleep(0)
        found = self.actions.get(action_id)
        return (
            found
            if found is not None and found.organization_id == context.organization_id
            else None
        )

    async def stored_attachment(
        self, context: ScopeContext, attachment_id: uuid.UUID
    ) -> StoredAttachment | None:
        if self.pause:
            await asyncio.sleep(0)
        found = self.attachments.get(attachment_id)
        return (
            found
            if found is not None and found.organization_id == context.organization_id
            else None
        )


@dataclass(frozen=True)
class AuditEntry:
    operation: str
    outcome: str
    resource_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    zone_id: uuid.UUID | None
    filters: dict[str, Any] | None
    result_count: int | None


@dataclass
class RecordingAudit:
    entries: list[AuditEntry] = field(default_factory=list)
    pause: bool = False

    async def append(self, context: ScopeContext, operation: Any, **fields: Any) -> None:
        if self.pause:
            await asyncio.sleep(0)
        resource = fields.get("resource")
        self.entries.append(
            AuditEntry(
                operation=str(operation),
                outcome=str(fields.get("outcome", "success")),
                resource_id=None if resource is None else resource.id,
                plant_id=fields.get("plant_id"),
                zone_id=fields.get("zone_id"),
                filters=None if fields.get("filters") is None else dict(fields["filters"]),
                result_count=fields.get("result_count"),
            )
        )

    def successes(self) -> list[AuditEntry]:
        return [entry for entry in self.entries if entry.outcome == "success"]


@dataclass
class DeniedLog:
    """``AuthorizationAudit`` en memoria: las denegaciones de ``Authorizer``."""

    denied: list[tuple[PermissionKey, Resource]] = field(default_factory=list)

    async def authorization_denied(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> None:
        self.denied.append((key, resource))


@dataclass
class AttachmentWorld:
    clock: Clock
    store: MemoryStore
    repository: MemoryRepository
    audit: RecordingAudit
    denied: DeniedLog
    service: ClosureAttachmentService


def attachment_world(
    clock: Clock, *, budget: Budget = CLOSURE_ATTACHMENT_BUDGET
) -> AttachmentWorld:
    """El puerto real (``build_attachment_service``) con almacén, proyección y auditoría falsos.

    La autorización es la real (``Authorizer`` sobre la matriz): la zona decide.
    """
    store = MemoryStore(clock)
    repository = MemoryRepository()
    audit = RecordingAudit()
    denied = DeniedLog()
    service = build_attachment_service(
        repository=repository,
        storage=store,
        authorizer=Authorizer(audit=denied, provider_organization_id=uuid.uuid4()),
        audit=audit,
        clock=clock,
        budget=budget,
    )
    return AttachmentWorld(clock, store, repository, audit, denied, service)


def utc(year: int = 2026, month: int = 10, day: int = 18) -> datetime:
    from datetime import UTC

    return datetime(year, month, day, tzinfo=UTC)
