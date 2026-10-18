"""Inspección acotada de los bytes de un adjunto de cierre (PAT-LAZ-SEG-05, pasos 3 y 4).

Funciones puras sobre **dos ventanas** del objeto, nunca sobre el objeto entero:

- el **prefijo** (``PREFIX_WINDOW_BYTES`` como mucho), donde viven la firma de bytes y los
  metadatos de una imagen (segmentos ``APPn`` de JPEG antes de ``SOS``; fragmentos de PNG antes
  del primer ``IDAT``);
- el **sufijo** (``SUFFIX_WINDOW_BYTES`` como mucho), donde un JPEG puede llevar imágenes
  secundarias tras ``EOI`` y un PNG fragmentos auxiliares tras los ``IDAT``.

Coordenadas de posición (``attachment_metadata_not_allowed``), **sin limpiar nada**:

- EXIF (``APP1`` ``Exif\\0\\0`` de JPEG o fragmento ``eXIf`` de PNG): se recorre la estructura
  TIFF y se rechaza si el IFD de GPS (etiqueta ``0x8825``) trae latitud o longitud (etiquetas 1 a
  4);
- XMP y texto (``APP1`` XMP, ``tEXt``, ``zTXt`` e ``iTXt``): se rechaza si nombra
  ``GPSLatitude`` o ``GPSLongitude``; el texto comprimido se descomprime con tope.

**Fallo cerrado**: una estructura que no se puede recorrer dentro de la ventana (truncada,
desplazamientos fuera de rango, metadatos que no caben en el prefijo, texto comprimido que no se
descomprime o que supera el tope) cuenta como metadatos no permitidos: lo que no se puede
comprobar no entra al expediente. En el sufijo, cualquier rastro de EXIF, de GPS o de texto
comprimido después de la zona recorrida también rechaza.

Límite declarado: lo que quede entre el final del prefijo recorrido y el comienzo del sufijo no
se lee (sería descargar el objeto). Un PDF solo pasa por la firma de bytes: el diseño rechaza
coordenadas en **imágenes**.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping
from typing import Final, Literal

from vigia_platform.loop.domain.enums import AttachmentContentType

__all__ = [
    "PREFIX_WINDOW_BYTES",
    "SUFFIX_WINDOW_BYTES",
    "has_position_metadata",
    "signature_matches",
]

PREFIX_WINDOW_BYTES: Final = 256 * 1024
"""Bytes del principio del objeto que se leen ``[objetivo propio]``."""
SUFFIX_WINDOW_BYTES: Final = 64 * 1024
"""Bytes del final del objeto que se leen ``[objetivo propio]``."""

_TEXT_LIMIT_BYTES: Final = 1024 * 1024
"""Tope del texto descomprimido de un fragmento de PNG."""
_MAX_IFD_ENTRIES: Final = 512
_MAX_IFD_CHAIN: Final = 8

_SIGNATURES: Final[Mapping[AttachmentContentType, bytes]] = {
    AttachmentContentType.IMAGE_JPEG: b"\xff\xd8\xff",
    AttachmentContentType.IMAGE_PNG: b"\x89PNG\r\n\x1a\n",
    AttachmentContentType.APPLICATION_PDF: b"%PDF-",
}
_EXIF_HEADER: Final = b"Exif\x00\x00"
_GPS_NAMES: Final = (b"GPSLatitude", b"GPSLongitude")
_GPS_IFD_TAG: Final = 0x8825
_GPS_POSITION_TAGS: Final = frozenset({1, 2, 3, 4})
_SUFFIX_MARKERS: Final = (_EXIF_HEADER, b"eXIf", b"zTXt", b"iTXt", *_GPS_NAMES)

type _Endian = Literal["little", "big"]


def signature_matches(content_type: AttachmentContentType, prefix: bytes) -> bool:
    """Los primeros bytes son los del tipo declarado (lista de permitidos)."""
    return prefix.startswith(_SIGNATURES[content_type])


def has_position_metadata(
    content_type: AttachmentContentType, prefix: bytes, suffix: bytes, size_bytes: int
) -> bool:
    """``True`` si la imagen trae coordenadas **o** no se puede comprobar que no las trae.

    ``prefix`` empieza en el byte 0; ``suffix`` termina en el último byte del objeto de
    ``size_bytes`` bytes. Si el objeto cabe en el prefijo, ``suffix`` se ignora.
    """
    if content_type is AttachmentContentType.APPLICATION_PDF:
        return False
    whole = len(prefix) >= size_bytes
    if content_type is AttachmentContentType.IMAGE_JPEG:
        walked = _jpeg_metadata_end(prefix, whole=whole)
    else:
        walked = _png_metadata_end(prefix, whole=whole)
    if walked is None:
        return True
    if whole:
        return False
    # Del sufijo solo cuenta lo que queda después de la zona ya recorrida del prefijo.
    suffix_start = size_bytes - len(suffix)
    tail = suffix[max(0, walked - suffix_start) :]
    return any(marker in tail for marker in _SUFFIX_MARKERS)


# --- JPEG ---------------------------------------------------------------------------------------


def _jpeg_metadata_end(data: bytes, *, whole: bool) -> int | None:
    """Recorre los segmentos hasta ``SOS``; devuelve dónde acaba lo recorrido o ``None``.

    ``None`` si algún segmento trae coordenadas o la estructura no se puede recorrer.
    """
    offset = 2
    while True:
        if offset + 2 > len(data):
            return None
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            offset += 2
            continue
        if marker == 0xD9:
            return offset + 2
        if offset + 4 > len(data):
            return None
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        end = offset + 2 + length
        if length < 2 or end > len(data):
            return None
        segment = data[offset + 4 : end]
        if (0xE0 <= marker <= 0xEF or marker == 0xFE) and _segment_has_position(segment):
            return None
        if marker == 0xDA:
            if whole:
                # Con el objeto entero a la vista, se mira también lo que va tras el escaneo.
                rest = data[end:]
                return None if any(m in rest for m in _SUFFIX_MARKERS) else len(data)
            return end
        offset = end


def _segment_has_position(segment: bytes) -> bool:
    if segment.startswith(_EXIF_HEADER) and _tiff_has_position(segment[len(_EXIF_HEADER) :]):
        return True
    return any(name in segment for name in _GPS_NAMES)


# --- PNG ----------------------------------------------------------------------------------------


def _png_metadata_end(data: bytes, *, whole: bool) -> int | None:
    """Recorre los fragmentos hasta el primer ``IDAT``; ``None`` si hay o puede haber posición."""
    offset = 8
    while True:
        if offset + 8 > len(data):
            return None
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        if length > 0x7FFFFFFF:
            return None
        if kind in (b"IDAT", b"IEND"):
            if whole:
                rest = data[offset:]
                return None if any(m in rest for m in _SUFFIX_MARKERS) else len(data)
            return offset
        end = offset + 12 + length
        if end > len(data):
            return None
        body = data[offset + 8 : offset + 8 + length]
        if kind == b"eXIf" and _tiff_has_position(body):
            return None
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            text = _png_text(kind, body)
            if text is None or any(name in text for name in _GPS_NAMES):
                return None
        offset = end


def _png_text(kind: bytes, body: bytes) -> bytes | None:
    """El texto de un fragmento, descomprimido con tope; ``None`` si no se puede leer."""
    keyword_end = body.find(b"\x00")
    if keyword_end < 0:
        return None
    if kind == b"tEXt":
        return body
    if kind == b"zTXt":
        return _inflate(body[keyword_end + 2 :]) if len(body) > keyword_end + 1 else None
    # iTXt: palabra clave\0, marca de compresión, método, idioma\0, palabra traducida\0, texto.
    flags = body[keyword_end + 1 : keyword_end + 3]
    if len(flags) < 2:
        return None
    rest = body[keyword_end + 3 :]
    language_end = rest.find(b"\x00")
    translated_end = rest.find(b"\x00", language_end + 1) if language_end >= 0 else -1
    if translated_end < 0:
        return None
    text = rest[translated_end + 1 :]
    if flags[0] == 0:
        return body
    return _inflate(text)


def _inflate(data: bytes) -> bytes | None:
    inflater = zlib.decompressobj()
    try:
        text = inflater.decompress(data, _TEXT_LIMIT_BYTES)
    except zlib.error:
        return None
    if inflater.unconsumed_tail:
        return None
    return text


# --- TIFF (EXIF) --------------------------------------------------------------------------------


def _tiff_has_position(tiff: bytes) -> bool:
    """``True`` si el IFD de GPS trae latitud o longitud, o si la estructura no se lee."""
    if len(tiff) < 8:
        return True
    order = tiff[:2]
    if order == b"II":
        endian: _Endian = "little"
    elif order == b"MM":
        endian = "big"
    else:
        return True
    if int.from_bytes(tiff[2:4], endian) != 42:
        return True
    offset = int.from_bytes(tiff[4:8], endian)
    seen: set[int] = set()
    for _ in range(_MAX_IFD_CHAIN):
        if offset == 0:
            return False
        if offset in seen:
            return True
        seen.add(offset)
        entries = _ifd(tiff, offset, endian)
        if entries is None:
            return True
        for tag, value in entries[0]:
            if tag == _GPS_IFD_TAG:
                gps = _ifd(tiff, value, endian)
                if gps is None:
                    return True
                if any(gps_tag in _GPS_POSITION_TAGS for gps_tag, _ in gps[0]):
                    return True
        offset = entries[1]
    return True


def _ifd(tiff: bytes, offset: int, endian: _Endian) -> tuple[list[tuple[int, int]], int] | None:
    """Las entradas ``(etiqueta, valor de 4 bytes)`` de un IFD y el desplazamiento del siguiente."""
    if offset < 8 or offset + 2 > len(tiff):
        return None
    count = int.from_bytes(tiff[offset : offset + 2], endian)
    end = offset + 2 + 12 * count
    if count > _MAX_IFD_ENTRIES or end + 4 > len(tiff):
        return None
    entries = []
    for index in range(count):
        start = offset + 2 + 12 * index
        tag = int.from_bytes(tiff[start : start + 2], endian)
        value = int.from_bytes(tiff[start + 8 : start + 12], endian)
        entries.append((tag, value))
    return entries, int.from_bytes(tiff[end : end + 4], endian)
