"""Orden fijo de validación de los adjuntos de cierre (PAT-LAZ-SEG-05; PR-LAZ-11 parcial).

- Propiedad sobre ``attachment_objects``: con cualquier combinación de defectos, en el mismo
  adjunto o en adjuntos distintos del lote, el rechazo es el del **primer paso** del orden; sin
  defectos, el lote se acepta.
- Ningún rechazo deja registro: el verificador solo lee (``HEAD`` y dos ventanas acotadas) y no
  tiene con qué escribir; los bytes del almacén no cambian (no se limpian metadatos).
- Ejemplos de NFR-LAZ-19: un PNG renombrado a PDF, imágenes con coordenadas en EXIF (las dos
  ordenaciones de bytes), en ``eXIf``, en XMP y en texto comprimido de PNG, y el fallo cerrado
  cuando la estructura no se puede recorrer.
"""

from __future__ import annotations

import asyncio
import uuid
import zlib

import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from tests.attachment_support import (
    XMP_WITH_GPS,
    MemoryStore,
    jpeg,
    pdf,
    png,
    raw_profile,
    sha256_hex,
    tiff,
    utc,
)
from tests.properties.laz.strategies.attachments import (
    CODES,
    AttachmentCase,
    Step,
    attachment_objects,
)
from vigia_platform.loop.adapters.attachments.inspect import (
    PREFIX_WINDOW_BYTES,
    SUFFIX_WINDOW_BYTES,
    has_position_metadata,
)
from vigia_platform.loop.adapters.attachments.rules import (
    AttachmentRejected,
    AttachmentSchemaInvalid,
    attachment_storage_key,
)
from vigia_platform.loop.adapters.attachments.store import StoreCircuit
from vigia_platform.loop.adapters.attachments.verify import (
    AttachmentClaim,
    AttachmentSubject,
    ClosureAttachmentVerifier,
    verify_batch,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.storage import StorageUnavailable

_READS = {"head_object", "get_object_prefix", "get_object_suffix"}


def _outcome(case: AttachmentCase, run: object) -> None:
    expected = case.first
    if expected is None:
        run()  # type: ignore[operator]
        return
    if expected is Step.SCHEMA:
        with pytest.raises(AttachmentSchemaInvalid):
            run()  # type: ignore[operator]
        return
    with pytest.raises(AttachmentRejected) as raised:
        run()  # type: ignore[operator]
    assert raised.value.detail is CODES[expected], (case.defects, raised.value.detail)


@given(attachment_objects())
def test_the_first_failing_step_decides_the_code(case: AttachmentCase) -> None:
    event(f"primer paso: {case.first.name if case.first else 'ninguno'}")
    event(f"defectos: {len(case.defects)}")
    _outcome(
        case,
        lambda: verify_batch(case.claims, case.objects, already_accepted=case.already_accepted),
    )


def _verifier(case: AttachmentCase) -> tuple[ClosureAttachmentVerifier, MemoryStore]:
    clock = SimulatedClock(utc())
    store = MemoryStore(clock)
    for found in case.stored:
        if found is not None:
            store.put(*found)
    return ClosureAttachmentVerifier(store, StoreCircuit(clock)), store


@given(attachment_objects())
def test_the_verifier_reads_only_and_agrees_with_the_pure_order(case: AttachmentCase) -> None:
    verifier, store = _verifier(case)
    before = dict(store.objects)
    _outcome(
        case,
        lambda: asyncio.run(
            verifier.verify(case.subject, case.claims, already_accepted=case.already_accepted)
        ),
    )
    # Solo lecturas: nada se escribe ni se lista, y los bytes (con sus metadatos) no cambian.
    assert set(store.calls) <= _READS
    assert store.objects == before
    if case.first in (Step.SCHEMA, Step.DECLARED_TYPE):
        assert store.calls == []  # el esquema y el tipo declarado van antes de tocar el almacén


# --- Ejemplos -------------------------------------------------------------------------------------

SUBJECT = AttachmentSubject(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


def _verify(data: bytes, declared: AttachmentContentType, *, stored_as: str | None = None) -> None:
    clock = SimulatedClock(utc())
    store = MemoryStore(clock)
    attachment_id = uuid.uuid4()
    key = attachment_storage_key(
        organization_id=SUBJECT.organization_id,
        plant_id=SUBJECT.plant_id,
        zone_id=SUBJECT.zone_id,
        subject_id=SUBJECT.subject_id,
        attachment_id=attachment_id,
        content_type=declared,
    )
    store.put(key, data, stored_as or declared.value)
    claim = AttachmentClaim(
        attachment_id, declared.value, len(data), sha256_hex(data), "evidencia.bin", True
    )
    asyncio.run(ClosureAttachmentVerifier(store, StoreCircuit(clock)).verify(SUBJECT, [claim]))
    assert store.objects[key][0] == data  # sin limpiar


def _rejected(data: bytes, declared: AttachmentContentType) -> LoopDetailCode:
    with pytest.raises(AttachmentRejected) as raised:
        _verify(data, declared)
    return raised.value.detail


def test_a_png_renamed_to_pdf_is_rejected_by_its_byte_signature() -> None:
    assert (
        _rejected(png(), AttachmentContentType.APPLICATION_PDF)
        is LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED
    )


@pytest.mark.parametrize("big_endian", [False, True])
@pytest.mark.parametrize("tags", [(2,), (4,), (1, 2, 3, 4)])
def test_a_jpeg_with_gps_coordinates_is_rejected_without_cleaning(
    tags: tuple[int, ...], big_endian: bool
) -> None:
    data = jpeg(exif=tiff(gps_tags=tags, big_endian=big_endian))
    assert (
        _rejected(data, AttachmentContentType.IMAGE_JPEG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


@pytest.mark.parametrize(
    "data",
    [
        png(exif=tiff(gps_tags=(2, 4))),
        png(texts=((b"tEXt", b"XML:com.adobe.xmp\x00" + XMP_WITH_GPS),)),
        png(texts=((b"zTXt", b"XML:com.adobe.xmp\x00\x00" + zlib.compress(XMP_WITH_GPS)),)),
        png(after_idat=((b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + XMP_WITH_GPS),)),
    ],
    ids=["exif", "text", "compressed-text", "after-idat"],
)
def test_a_png_with_gps_coordinates_is_rejected(data: bytes) -> None:
    assert (
        _rejected(data, AttachmentContentType.IMAGE_PNG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


_EXIF_GPS = b"Exif\x00\x00" + tiff(gps_tags=(1, 2, 3, 4))


@pytest.mark.parametrize(
    "data",
    [
        png(texts=((b"tEXt", b"Raw profile type exif\x00" + raw_profile(b"exif", _EXIF_GPS)),)),
        png(
            texts=(
                (
                    b"zTXt",
                    b"Raw profile type exif\x00\x00"
                    + zlib.compress(raw_profile(b"exif", _EXIF_GPS)),
                ),
            )
        ),
        png(texts=((b"tEXt", b"Raw profile type APP1\x00" + raw_profile(b"APP1", _EXIF_GPS)),)),
        png(
            texts=(
                (b"tEXt", b"Raw profile type exif\x00" + raw_profile(b"exif", tiff(gps_tags=(2,)))),
            )
        ),
        png(texts=((b"tEXt", b"Raw profile type exif\x00\nexif\n  12\nno-es-hex"),)),
        png(after_idat=((b"tEXt", b"Raw profile type exif\x00" + raw_profile(b"exif", b"x")),)),
    ],
    ids=["text", "compressed-text", "app1", "bare-tiff", "broken-hex", "after-idat"],
)
def test_a_png_with_a_hex_raw_exif_profile_with_gps_is_rejected(data: bytes) -> None:
    """Revisión automática, ronda 1: el EXIF en hexadecimal de ImageMagick y exiftool."""
    assert (
        _rejected(data, AttachmentContentType.IMAGE_PNG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


def test_a_hex_raw_exif_profile_without_gps_is_accepted() -> None:
    profile = raw_profile(b"exif", b"Exif\x00\x00" + tiff(gps_tags=(0, 5)))
    data = png(texts=((b"tEXt", b"Raw profile type exif\x00" + profile),))
    _verify(data, AttachmentContentType.IMAGE_PNG)


@pytest.mark.parametrize("name", [b"GpsLatitude", b"gpslongitude", b"GPSLATITUDE"])
def test_gps_names_are_matched_in_any_case(name: bytes) -> None:
    xmp = b"<x:xmpmeta><rdf:Description exif:" + name + b'="4,36.0N"/></x:xmpmeta>'
    assert (
        _rejected(jpeg(xmp=xmp), AttachmentContentType.IMAGE_JPEG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


def test_an_xmp_position_in_a_jpeg_is_rejected() -> None:
    assert (
        _rejected(jpeg(xmp=XMP_WITH_GPS), AttachmentContentType.IMAGE_JPEG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


@pytest.mark.parametrize(
    "data",
    [
        jpeg(exif=tiff(gps_tags=None)),
        jpeg(exif=tiff(gps_tags=(0,), big_endian=True)),
        png(exif=tiff(gps_tags=(0, 5))),
        pdf(),
    ],
    ids=["exif-without-gps", "gps-version-only", "png-altitude-only", "pdf"],
)
def test_metadata_without_position_is_accepted(data: bytes) -> None:
    kind = {b"\xff": "image/jpeg", b"\x89": "image/png", b"%": "application/pdf"}[data[:1]]
    _verify(data, AttachmentContentType(kind))


@pytest.mark.parametrize(
    "exif",
    [
        b"II*\x00",  # TIFF truncado
        b"II*\x00\xff\xff\x00\x00",  # IFD0 fuera de rango
        b"XX*\x00\x08\x00\x00\x00",  # orden de bytes desconocido
        tiff(gps_tags=(2,))[:30],  # IFD de GPS cortado
    ],
    ids=["truncated", "offset", "byte-order", "cut-gps-ifd"],
)
def test_an_unreadable_exif_fails_closed(exif: bytes) -> None:
    assert (
        _rejected(jpeg(exif=exif), AttachmentContentType.IMAGE_JPEG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


def test_metadata_that_does_not_fit_the_prefix_window_fails_closed() -> None:
    # Un APP2 enorme antes del EXIF: el recorrido sale de la ventana sin llegar a SOS.
    filler = b"\xff\xe2\xff\xff" + b"\x00" * 0xFFFD
    data = b"\xff\xd8" + filler * 5 + jpeg()[2:] + b"\x00" * PREFIX_WINDOW_BYTES
    assert len(data) > PREFIX_WINDOW_BYTES
    assert has_position_metadata(
        AttachmentContentType.IMAGE_JPEG,
        data[:PREFIX_WINDOW_BYTES],
        data[-SUFFIX_WINDOW_BYTES:],
        len(data),
    )


def test_a_secondary_image_with_exif_after_eoi_is_rejected_from_the_suffix() -> None:
    secondary = jpeg(exif=tiff(gps_tags=(2, 4)))
    data = jpeg(scan=b"\x55" * (PREFIX_WINDOW_BYTES * 2), trailer=secondary)
    assert (
        _rejected(data, AttachmentContentType.IMAGE_JPEG)
        is LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED
    )


def test_a_large_clean_image_is_read_by_two_bounded_windows_only() -> None:
    clock = SimulatedClock(utc())
    store = MemoryStore(clock)
    data = jpeg(exif=tiff(gps_tags=None), scan=b"\x55" * (3 * 1024 * 1024))
    attachment_id = uuid.uuid4()
    key = attachment_storage_key(
        organization_id=SUBJECT.organization_id,
        plant_id=SUBJECT.plant_id,
        zone_id=SUBJECT.zone_id,
        subject_id=SUBJECT.subject_id,
        attachment_id=attachment_id,
        content_type=AttachmentContentType.IMAGE_JPEG,
    )
    store.put(key, data, "image/jpeg")
    claim = AttachmentClaim(attachment_id, "image/jpeg", len(data), sha256_hex(data), "a.jpg", True)
    verified = asyncio.run(
        ClosureAttachmentVerifier(store, StoreCircuit(clock)).verify(SUBJECT, [claim])
    )
    assert [item.storage_key for item in verified] == [key]
    assert verified[0].version_id == "v1"
    assert store.calls == ["head_object", "get_object_prefix", "get_object_suffix"]


def test_an_empty_batch_never_touches_the_store_even_with_the_circuit_open() -> None:
    clock = SimulatedClock(utc())
    store = MemoryStore(clock, down=True)
    circuit = StoreCircuit(clock)
    verifier = ClosureAttachmentVerifier(store, circuit)
    claim = AttachmentClaim(uuid.uuid4(), "image/png", 10, "a" * 64, "a.png", True)
    with pytest.raises(StorageUnavailable):
        asyncio.run(verifier.verify(SUBJECT, [claim]))
    calls = len(store.calls)
    # Con el circuito abierto, la descripción estructurada sigue bastando (FS-LAZ-03).
    assert asyncio.run(verifier.verify(SUBJECT, [])) == ()
    assert len(store.calls) == calls


@given(st.integers(min_value=0, max_value=25), st.integers(min_value=0, max_value=6))
def test_the_entry_and_finding_caps_are_exact(already: int, count: int) -> None:
    clock = SimulatedClock(utc())
    store = MemoryStore(clock)
    claims = []
    for _ in range(count):
        data, attachment_id = pdf(), uuid.uuid4()
        store.put(
            attachment_storage_key(
                organization_id=SUBJECT.organization_id,
                plant_id=SUBJECT.plant_id,
                zone_id=SUBJECT.zone_id,
                subject_id=SUBJECT.subject_id,
                attachment_id=attachment_id,
                content_type=AttachmentContentType.APPLICATION_PDF,
            ),
            data,
            "application/pdf",
        )
        claims.append(
            AttachmentClaim(
                attachment_id, "application/pdf", len(data), sha256_hex(data), "d", True
            )
        )
    verifier = ClosureAttachmentVerifier(store, StoreCircuit(clock))
    if count > 5 or already + count > 20:
        with pytest.raises(AttachmentRejected) as raised:
            asyncio.run(verifier.verify(SUBJECT, claims, already_accepted=already))
        assert raised.value.detail is LoopDetailCode.ATTACHMENT_TOO_LARGE
    else:
        assert len(asyncio.run(verifier.verify(SUBJECT, claims, already_accepted=already))) == count
