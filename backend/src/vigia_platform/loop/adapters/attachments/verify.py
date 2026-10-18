"""``ClosureAttachmentPort.verify``: el orden fijo de PAT-LAZ-SEG-05 sobre un lote de adjuntos.

Lo llama quien va a escribir el registro que referencia los adjuntos (``action_evidence_added``,
TASK-311) **antes** de escribirlo. Nunca escribe nada: ni en el expediente, ni en la auditoría,
ni en el almacén; cualquier fallo rechaza **el lote entero** y no queda registro.

Los pasos se aplican **a todo el lote, uno tras otro**: con varios defectos, en el mismo adjunto
o en adjuntos distintos, gana el del primer paso del orden.

1. Esquema (``AttachmentSchemaInvalid``, 400 sin código): identificador UUID sin repetir,
   cadenas y enteros de su tipo JSON, ``sha256`` hexadecimal de 64, nombre de archivo de 1 a 128
   sin ruta ni caracteres de control, declaración booleana, como mucho
   ``MAX_ATTACHMENTS_PER_FINDING`` adjuntos en la petición.
2. Tipo declarado en ``attachment_content_type`` → ``loop_attachment_type_not_allowed``.
3. Firma de bytes y tipo del objeto (el que se firmó en la concesión) →
   ``loop_attachment_type_not_allowed``.
4. Coordenadas de posición en una imagen (``inspect.has_position_metadata``), **sin limpiar los
   metadatos** → ``loop_attachment_metadata_not_allowed``.
5. Tamaño: 10 MB por adjunto (declarado y del objeto), 5 por entrada y 20 por hallazgo contando
   los ya aceptados → ``loop_attachment_too_large``.
6. ``sha256`` y tamaño **contra el objeto almacenado** (la suma la calcula el almacén y la
   devuelve ``HEAD``) → ``loop_attachment_hash_mismatch``. Un objeto ausente falla aquí: los
   pasos 3 y 4 no se le aplican porque no hay bytes.
7. Declaración firmada de que no contiene persona identificable →
   ``loop_attachment_declaration_missing``.

**Sin descargar**: del almacén solo se leen los metadatos (``HEAD``) y dos ventanas acotadas de
la versión verificada (``get_object_prefix`` y ``get_object_suffix``); nunca el objeto entero.
Todo pasa por el cortacircuito: con el circuito abierto, ``StorageUnavailable``. Un lote vacío
no toca el almacén (el cierre con la descripción estructurada sigue siendo posible, FS-LAZ-03).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from vigia_platform.loop.adapters.attachments.inspect import (
    PREFIX_WINDOW_BYTES,
    SUFFIX_WINDOW_BYTES,
    has_position_metadata,
    signature_matches,
)
from vigia_platform.loop.adapters.attachments.rules import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_ENTRY,
    MAX_ATTACHMENTS_PER_FINDING,
    AttachmentRejected,
    AttachmentSchemaInvalid,
    attachment_storage_key,
)
from vigia_platform.loop.adapters.attachments.store import ClosureObjectStorage, StoreCircuit
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.shared.storage import ObjectHead

__all__ = [
    "AttachmentClaim",
    "AttachmentObject",
    "AttachmentSubject",
    "ClosureAttachmentVerifier",
    "VerifiedAttachment",
    "verify_batch",
]

_MAX_FILENAME_CHARS: Final = 128
_SHA256_CHARS: Final = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class AttachmentClaim:
    """Lo que declara quien referencia un adjunto, tal cual llega (sin validar)."""

    attachment_id: object
    content_type: object
    size_bytes: object
    sha256: object
    filename: object
    no_identifiable_person_declared: object


@dataclass(frozen=True, slots=True)
class AttachmentSubject:
    """El lugar del registro: la clave se construye desde aquí, nunca desde el cliente."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AttachmentObject:
    """Lo leído del almacén para un adjunto: ``None`` si no hay objeto en su clave."""

    head: ObjectHead | None
    prefix: bytes = field(default=b"", repr=False)
    suffix: bytes = field(default=b"", repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedAttachment:
    """Un adjunto aceptado, con la versión exacta del objeto verificado."""

    attachment_id: uuid.UUID
    content_type: AttachmentContentType
    size_bytes: int
    sha256: str
    filename: str
    storage_key: str
    version_id: str | None


@dataclass(frozen=True, slots=True)
class _Parsed:
    attachment_id: uuid.UUID
    content_type: str
    size_bytes: int
    sha256: str
    filename: str
    declared: bool


def _schema(claims: object, already_accepted: object) -> tuple[tuple[_Parsed, ...], int]:
    """Paso 1: la forma, antes de cualquier otra cosa; los adjuntos y los ya aceptados."""
    if type(already_accepted) is not int or already_accepted < 0:
        raise AttachmentSchemaInvalid("already_accepted debe ser un entero no negativo")
    if not isinstance(claims, Sequence) or isinstance(claims, str | bytes):
        raise AttachmentSchemaInvalid("los adjuntos deben ser una lista")
    if len(claims) > MAX_ATTACHMENTS_PER_FINDING:
        raise AttachmentSchemaInvalid("demasiados adjuntos en una petición")
    parsed: list[_Parsed] = []
    for claim in claims:
        if not isinstance(claim, AttachmentClaim):
            raise AttachmentSchemaInvalid("adjunto con forma inválida")
        attachment_id, content_type = claim.attachment_id, claim.content_type
        size, digest, filename = claim.size_bytes, claim.sha256, claim.filename
        declared = claim.no_identifiable_person_declared
        if (
            not isinstance(attachment_id, uuid.UUID)
            or not isinstance(content_type, str)
            or not 1 <= len(content_type) <= 255
            or type(size) is not int
            or size < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or not set(digest) <= _SHA256_CHARS
            or not isinstance(filename, str)
            or not 1 <= len(filename) <= _MAX_FILENAME_CHARS
            or any(char in "/\\" or not char.isprintable() for char in filename)
            or type(declared) is not bool
        ):
            raise AttachmentSchemaInvalid("adjunto con forma inválida")
        parsed.append(
            _Parsed(
                uuid.UUID(int=attachment_id.int), content_type, size, digest, filename, declared
            )
        )
    if len({item.attachment_id for item in parsed}) != len(parsed):
        raise AttachmentSchemaInvalid("adjunto repetido en la petición")
    return tuple(parsed), already_accepted


def _declared_types(parsed: Sequence[_Parsed]) -> tuple[AttachmentContentType, ...]:
    """Paso 2: tipo declarado dentro de la lista cerrada."""
    types: list[AttachmentContentType] = []
    for item in parsed:
        try:
            types.append(AttachmentContentType(item.content_type))
        except ValueError:
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED) from None
    return tuple(types)


def verify_batch(
    claims: object, objects: Sequence[AttachmentObject], *, already_accepted: object = 0
) -> None:
    """Los siete pasos sobre un lote y lo leído de cada objeto (en el mismo orden); puro.

    Lanza el rechazo del primer paso que falla y no devuelve nada si el lote se acepta.
    """
    parsed, accepted = _schema(claims, already_accepted)
    types = _declared_types(parsed)
    if len(objects) != len(parsed):
        raise ValueError("un objeto leído por adjunto")
    _steps(parsed, types, objects, already_accepted=accepted)


def _steps(
    parsed: Sequence[_Parsed],
    types: Sequence[AttachmentContentType],
    objects: Sequence[AttachmentObject],
    *,
    already_accepted: int,
) -> None:
    """Pasos 3 a 7 sobre lo ya leído; lanza el rechazo del primer paso que falla."""
    pairs = list(zip(parsed, types, objects, strict=True))
    # 3. Firma de bytes y tipo del objeto.
    for _, kind, found in pairs:
        if found.head is not None and (
            found.head.content_type != kind.value or not signature_matches(kind, found.prefix)
        ):
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED)
    # 4. Coordenadas de posición en las imágenes.
    for _, kind, found in pairs:
        if found.head is not None and has_position_metadata(
            kind, found.prefix, found.suffix, found.head.size_bytes
        ):
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED)
    # 5. Tamaño por adjunto y topes por entrada y por hallazgo.
    if (
        len(pairs) > MAX_ATTACHMENTS_PER_ENTRY
        or already_accepted + len(pairs) > MAX_ATTACHMENTS_PER_FINDING
        or any(
            item.size_bytes > MAX_ATTACHMENT_BYTES
            or (found.head is not None and found.head.size_bytes > MAX_ATTACHMENT_BYTES)
            for item, _, found in pairs
        )
    ):
        raise AttachmentRejected(LoopDetailCode.ATTACHMENT_TOO_LARGE)
    # 6. Suma y tamaño contra el objeto almacenado.
    for item, _, found in pairs:
        if (
            found.head is None
            or found.head.full_object_sha256_hex != item.sha256
            or found.head.size_bytes != item.size_bytes
        ):
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_HASH_MISMATCH)
    # 7. Declaración firmada.
    if not all(item.declared for item, _, _ in pairs):
        raise AttachmentRejected(LoopDetailCode.ATTACHMENT_DECLARATION_MISSING)


@dataclass(frozen=True, slots=True)
class ClosureAttachmentVerifier:
    """Lee solo metadatos y dos ventanas acotadas por adjunto; no escribe nada."""

    storage: ClosureObjectStorage = field(repr=False)
    circuit: StoreCircuit

    async def verify(
        self, subject: AttachmentSubject, claims: object, *, already_accepted: object = 0
    ) -> tuple[VerifiedAttachment, ...]:
        """Los adjuntos aceptados, en el orden recibido; si alguno falla, nada."""
        parsed, accepted = _schema(claims, already_accepted)
        types = _declared_types(parsed)
        keys = [
            attachment_storage_key(
                organization_id=subject.organization_id,
                plant_id=subject.plant_id,
                zone_id=subject.zone_id,
                subject_id=subject.subject_id,
                attachment_id=item.attachment_id,
                content_type=kind,
            )
            for item, kind in zip(parsed, types, strict=True)
        ]
        objects = [await self._read(key) for key in keys]
        _steps(parsed, types, objects, already_accepted=accepted)
        return tuple(
            VerifiedAttachment(
                attachment_id=item.attachment_id,
                content_type=kind,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                filename=item.filename,
                storage_key=key,
                version_id=None if found.head is None else found.head.version_id,
            )
            for item, kind, key, found in zip(parsed, types, keys, objects, strict=True)
        )

    async def _read(self, key: str) -> AttachmentObject:
        async def head() -> ObjectHead | None:
            return await self.storage.head_object(key)

        found = await self.circuit.call(head)
        if found is None:
            return AttachmentObject(None)
        version = found.version_id

        async def prefix() -> bytes:
            return await self.storage.get_object_prefix(
                key, PREFIX_WINDOW_BYTES, version_id=version
            )

        first = await self.circuit.call(prefix)
        if len(first) >= found.size_bytes:
            return AttachmentObject(found, first, b"")

        async def suffix() -> bytes:
            return await self.storage.get_object_suffix(
                key, SUFFIX_WINDOW_BYTES, version_id=version
            )

        return AttachmentObject(found, first, await self.circuit.call(suffix))
