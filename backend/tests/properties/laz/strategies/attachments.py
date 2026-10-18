"""Generador ``attachment_objects`` (business-logic-model §11; PR-LAZ-11 parcial, PAT-LAZ-SEG-05).

Mismo patrón que ``evidence_objects`` de U-02: lotes de 1 a 5 adjuntos **correctos e incorrectos**
(tipos, tamaños, firmas de bytes, metadatos de posición, sumas y declaraciones), con los bytes
sintéticos de ``tests.attachment_support`` y lo que devolvería el almacén por cada uno. Cada caso
lleva los defectos que se le inyectaron; el código esperado es el del **primer paso** del orden
fijo entre ellos (o ninguno).

Un defecto nunca introduce otro de un paso **anterior**: así, el primero del orden es el que
manda aunque un defecto arrastre otros posteriores (un tamaño declarado de más de 10 MB tampoco
coincide con el objeto, paso 6).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from hypothesis import strategies as st

from tests.attachment_support import (
    XMP_WITH_GPS,
    XMP_WITHOUT_GPS,
    jpeg,
    object_head,
    pdf,
    png,
    sha256_hex,
    tiff,
)
from vigia_platform.loop.adapters.attachments.inspect import (
    PREFIX_WINDOW_BYTES,
    SUFFIX_WINDOW_BYTES,
)
from vigia_platform.loop.adapters.attachments.rules import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_ENTRY,
    MAX_ATTACHMENTS_PER_FINDING,
    attachment_storage_key,
)
from vigia_platform.loop.adapters.attachments.verify import (
    AttachmentClaim,
    AttachmentObject,
    AttachmentSubject,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType

__all__ = ["AttachmentCase", "Step", "attachment_objects", "valid_attachment"]


class Step(enum.IntEnum):
    """Los siete pasos de PAT-LAZ-SEG-05, en su orden."""

    SCHEMA = 1
    DECLARED_TYPE = 2
    SIGNATURE = 3
    METADATA = 4
    SIZE = 5
    HASH = 6
    DECLARATION = 7


CODES: dict[Step, LoopDetailCode | None] = {
    Step.SCHEMA: None,
    Step.DECLARED_TYPE: LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED,
    Step.SIGNATURE: LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED,
    Step.METADATA: LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED,
    Step.SIZE: LoopDetailCode.ATTACHMENT_TOO_LARGE,
    Step.HASH: LoopDetailCode.ATTACHMENT_HASH_MISMATCH,
    Step.DECLARATION: LoopDetailCode.ATTACHMENT_DECLARATION_MISSING,
}


@dataclass(frozen=True)
class AttachmentCase:
    subject: AttachmentSubject
    claims: tuple[AttachmentClaim, ...]
    objects: tuple[AttachmentObject, ...]
    stored: tuple[tuple[str, bytes, str] | None, ...]
    """``(clave, bytes, tipo)`` de cada objeto en el almacén, o ``None`` si no se subió."""
    already_accepted: int
    defects: frozenset[Step]

    @property
    def first(self) -> Step | None:
        return min(self.defects) if self.defects else None


_FILENAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_ ", min_size=1, max_size=40)


@st.composite
def _clean_bytes(draw: st.DrawFn, kind: AttachmentContentType) -> bytes:
    """Bytes del tipo, con metadatos inocuos al azar (EXIF sin GPS, IFD de GPS vacío, XMP)."""
    if kind is AttachmentContentType.APPLICATION_PDF:
        return pdf(draw(st.binary(max_size=64)).hex().encode())
    exif = draw(
        st.sampled_from(
            [None, tiff(gps_tags=None), tiff(gps_tags=(0,)), tiff(gps_tags=None, big_endian=True)]
        )
    )
    if kind is AttachmentContentType.IMAGE_JPEG:
        return jpeg(exif=exif, xmp=draw(st.sampled_from([None, XMP_WITHOUT_GPS])))
    return png(exif=exif, texts=draw(st.sampled_from([(), ((b"tEXt", b"Comment\x00ok"),)])))


@st.composite
def _gps_bytes(draw: st.DrawFn, kind: AttachmentContentType) -> bytes:
    """Una imagen con coordenadas en algún sitio de sus metadatos."""
    tags = draw(st.sampled_from([(2,), (4,), (1, 2, 3, 4), (0, 2, 4)]))
    big = draw(st.booleans())
    if kind is AttachmentContentType.IMAGE_JPEG:
        return draw(
            st.sampled_from(
                [jpeg(exif=tiff(gps_tags=tags, big_endian=big)), jpeg(xmp=XMP_WITH_GPS)]
            )
        )
    return draw(
        st.sampled_from(
            [
                png(exif=tiff(gps_tags=tags, big_endian=big)),
                png(texts=((b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + XMP_WITH_GPS),)),
            ]
        )
    )


def _other_kind(kind: AttachmentContentType) -> bytes:
    """Bytes de otro tipo permitido: un PNG renombrado a PDF, un PDF renombrado a JPEG…"""
    return {
        AttachmentContentType.IMAGE_JPEG: png(),
        AttachmentContentType.IMAGE_PNG: pdf(),
        AttachmentContentType.APPLICATION_PDF: png(),
    }[kind]


def _breakage(draw: st.DrawFn, claim: dict[str, object]) -> None:
    """Un defecto de esquema: tipo JSON, suma, nombre o identificador con otra forma."""
    field_name = draw(
        st.sampled_from(["size_bytes", "sha256", "filename", "attachment_id", "flag"])
    )
    claim[field_name if field_name != "flag" else "no_identifiable_person_declared"] = {
        "size_bytes": draw(st.sampled_from([True, 0, -1, "12", 1.5])),
        "sha256": draw(st.sampled_from(["A" * 64, "a" * 63, "", "g" * 64])),
        "filename": draw(st.sampled_from(["", "a/b.jpg", "..\\x.png", "a\x00b", "x" * 129])),
        "attachment_id": draw(st.sampled_from(["no-uuid", 7, None])),
        "flag": draw(st.sampled_from([1, "true", None])),
    }[field_name]


@st.composite
def valid_attachment(
    draw: st.DrawFn, subject: AttachmentSubject
) -> tuple[AttachmentClaim, AttachmentObject, tuple[str, bytes, str]]:
    """Un adjunto correcto y su objeto en el almacén."""
    kind = draw(st.sampled_from(list(AttachmentContentType)))
    data = draw(_clean_bytes(kind))
    attachment_id = draw(st.uuids(version=4))
    key = _key(subject, attachment_id, kind)
    claim = AttachmentClaim(
        attachment_id, kind.value, len(data), sha256_hex(data), draw(_FILENAMES), True
    )
    return claim, _object(key, data, kind), (key, data, kind.value)


def _key(subject: AttachmentSubject, attachment_id: uuid.UUID, kind: AttachmentContentType) -> str:
    return attachment_storage_key(
        organization_id=subject.organization_id,
        plant_id=subject.plant_id,
        zone_id=subject.zone_id,
        subject_id=subject.subject_id,
        attachment_id=attachment_id,
        content_type=kind,
    )


def _object(key: str, data: bytes, kind: AttachmentContentType) -> AttachmentObject:
    head = object_head(key, data, kind.value)
    prefix = data[:PREFIX_WINDOW_BYTES]
    suffix = b"" if len(prefix) >= len(data) else data[-SUFFIX_WINDOW_BYTES:]
    return AttachmentObject(head, prefix, suffix)


@st.composite
def attachment_objects(draw: st.DrawFn) -> AttachmentCase:
    """Un lote de 1 a 5 adjuntos con defectos al azar (o ninguno) y lo que hay en el almacén.

    Primero se elige el **paso** del primer defecto (o ninguno), con la misma probabilidad cada
    uno; después, defectos de pasos **posteriores** en cualquier adjunto.
    """
    target: Step | None = draw(st.sampled_from([None, *Step]))
    subject = AttachmentSubject(*(draw(st.uuids(version=4)) for _ in range(4)))
    count = draw(st.integers(1, MAX_ATTACHMENTS_PER_ENTRY))
    batch = "none"
    if target is Step.SIZE:
        batch = draw(st.sampled_from(["none", "entry", "finding"]))
    elif target is Step.SCHEMA:
        batch = draw(st.sampled_from(["none", "duplicate"]))
    carrier = draw(st.integers(0, count - 1)) if batch == "none" and target is not None else -1
    later: list[object] = (
        []
        if target is None
        else [step for step in Step if step > target] + (["missing"] if target <= Step.HASH else [])
    )
    defects: set[Step] = set()
    claims: list[AttachmentClaim] = []
    objects: list[AttachmentObject] = []
    stored: list[tuple[str, bytes, str] | None] = []
    for index in range(count):
        chosen: set[object] = (
            set(draw(st.sets(st.sampled_from(later), max_size=2))) if later else set()
        )
        if index == carrier:
            chosen.add(target)
            if target in (Step.SIGNATURE, Step.METADATA):
                chosen.discard("missing")
        if target is Step.METADATA and index == carrier:
            kind = draw(
                st.sampled_from([AttachmentContentType.IMAGE_JPEG, AttachmentContentType.IMAGE_PNG])
            )
        else:
            kind = draw(st.sampled_from(list(AttachmentContentType)))
        missing = "missing" in chosen
        if missing:
            # Sin objeto no hay bytes: los pasos 3 y 4 no se le aplican; falla en el 6.
            chosen -= {"missing", Step.SIGNATURE, Step.METADATA}
            chosen.add(Step.HASH)
        if kind is AttachmentContentType.APPLICATION_PDF:
            chosen.discard(Step.METADATA)  # el PDF no pasa por el paso 4
        data = draw(_clean_bytes(kind))
        if Step.METADATA in chosen:
            data = draw(_gps_bytes(kind))
        if Step.SIGNATURE in chosen:
            data = _other_kind(kind)
        attachment_id = draw(st.uuids(version=4))
        key = _key(subject, attachment_id, kind)
        claim: dict[str, object] = {
            "attachment_id": attachment_id,
            "content_type": "image/gif" if Step.DECLARED_TYPE in chosen else kind.value,
            "size_bytes": MAX_ATTACHMENT_BYTES + 1 if Step.SIZE in chosen else len(data),
            "sha256": sha256_hex(data + b"!")
            if Step.HASH in chosen and not missing
            else sha256_hex(data),
            "filename": draw(_FILENAMES),
            "no_identifiable_person_declared": Step.DECLARATION not in chosen,
        }
        if Step.SCHEMA in chosen:
            _breakage(draw, claim)
        claims.append(AttachmentClaim(**claim))  # type: ignore[arg-type]
        objects.append(AttachmentObject(None) if missing else _object(key, data, kind))
        stored.append(None if missing else (key, data, kind.value))
        defects |= {step for step in chosen if isinstance(step, Step)}
    already = draw(st.integers(0, MAX_ATTACHMENTS_PER_FINDING - count))
    if batch == "entry":
        while len(claims) <= MAX_ATTACHMENTS_PER_ENTRY:
            claim, found, where = draw(valid_attachment(subject))
            claims.append(claim)
            objects.append(found)
            stored.append(where)
        already = 0
        defects.add(Step.SIZE)
    elif batch == "finding":
        already = MAX_ATTACHMENTS_PER_FINDING - len(claims) + 1
        defects.add(Step.SIZE)
    elif batch == "duplicate":
        claims.append(claims[0])
        objects.append(objects[0])
        stored.append(stored[0])
        defects.add(Step.SCHEMA)
    return AttachmentCase(
        subject, tuple(claims), tuple(objects), tuple(stored), already, frozenset(defects)
    )
