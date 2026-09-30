"""Marca de anonimización dentro del contenedor MP4 (BR-CTR-14; NFR-NUC-33, PAT-NUC-MAN-04).

La marca canónica es la etiqueta ``comment`` del contenedor con el valor exacto
``vigia_anonymized=1`` (nota del 2026-09-23 de BR-CTR-14). ``ffmpeg`` la escribe en
``moov/udta/meta/ilst/©cmt/data`` (elemento de metadatos de tipo UTF-8); QuickTime la admite
también como ``moov/udta/©cmt`` (longitud, idioma y texto). Se leen las dos, como el stub de
conformidad de U-01.

``read_container_marker(content)`` recorre las cajas **sin decodificar nada** y responde:

- ``PRESENT``: algún comentario es exactamente la marca (sin contar ceros finales de relleno);
- ``ABSENT``: el contenedor se lee entero y ningún comentario es la marca (falta o lleva otro
  valor, p. ej. ``vigia_anonymized=0``);
- ``UNREADABLE``: no es un MP4 legible: sin ``ftyp`` al principio, sin ``moov``, o con una caja
  mal formada (tamaño imposible, truncada o que se sale de su caja madre) en el nivel superior o
  en el camino de la marca. Fallo cerrado: lo que no se puede leer no cuenta como marcado.

Función pura sobre bytes, sin E/S ni reloj; el tamaño lo acota quien descarga (el máximo de un
clip del contrato).
"""

from __future__ import annotations

import enum
import struct
from collections.abc import Iterator
from typing import Final

__all__ = ["CONTAINER_MARK", "ContainerMarker", "read_container_marker"]

CONTAINER_MARK: Final = b"vigia_anonymized=1"
"""Valor de la etiqueta ``comment`` del contenedor (BR-CTR-14)."""

_FTYP: Final = b"ftyp"
_MOOV: Final = b"moov"
_UDTA: Final = b"udta"
_META: Final = b"meta"
_ILST: Final = b"ilst"
_HDLR: Final = b"hdlr"
_DATA: Final = b"data"
_COMMENT: Final = b"\xa9cmt"
_UTF8_DATA: Final = 1
"""Tipo de ``data`` de un elemento de metadatos con texto UTF-8 (tabla de tipos de QuickTime)."""


class ContainerMarker(enum.StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


class _Malformed(Exception):
    """Caja mal formada en el camino que se lee."""


def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Cajas de ``data[start:end]`` como ``(tipo, inicio del cuerpo, fin)``.

    Recorre hasta ``end`` exactamente: un resto de menos de 8 bytes, un tamaño menor que su
    cabecera o mayor que lo que queda lanza ``_Malformed``. ``size == 0`` (hasta el final) solo
    vale en la última caja del nivel superior, que es donde la define ISO/IEC 14496-12.
    """
    offset = start
    while offset < end:
        if end - offset < 8:
            raise _Malformed
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if end - offset < 16:
                raise _Malformed
            (size,) = struct.unpack_from(">Q", data, offset + 8)
            header = 16
        elif size == 0:
            if start != 0 or end != len(data):
                raise _Malformed
            size = end - offset
        if size < header or size > end - offset:
            raise _Malformed
        yield kind, offset + header, offset + size
        offset += size


def _children(data: bytes, start: int, end: int, kind: bytes) -> Iterator[tuple[int, int]]:
    for found, body_start, body_end in _boxes(data, start, end):
        if found == kind:
            yield body_start, body_end


def _meta_body(data: bytes, start: int, end: int) -> int:
    """Inicio de las cajas hijas de ``meta``: la ISO lleva 4 bytes de versión y banderas; la de
    QuickTime no, y su primera hija (``hdlr``) empieza de inmediato."""
    if end - start >= 8 and data[start + 4 : start + 8] == _HDLR:
        return start
    if end - start < 4:
        raise _Malformed
    return start + 4


def _comments(data: bytes) -> Iterator[bytes]:
    top = list(_boxes(data, 0, len(data)))
    if not top or top[0][0] != _FTYP:
        raise _Malformed
    movies = [(start, end) for kind, start, end in top if kind == _MOOV]
    if not movies:
        raise _Malformed
    for moov_start, moov_end in movies:
        for udta_start, udta_end in _children(data, moov_start, moov_end, _UDTA):
            for kind, start, end in _boxes(data, udta_start, udta_end):
                if kind == _COMMENT:
                    # QuickTime: longitud (2 bytes), idioma (2 bytes) y texto.
                    if end - start < 4:
                        raise _Malformed
                    (length,) = struct.unpack_from(">H", data, start)
                    if length > end - start - 4:
                        raise _Malformed
                    yield data[start + 4 : start + 4 + length]
                elif kind == _META:
                    body = _meta_body(data, start, end)
                    for ilst_start, ilst_end in _children(data, body, end, _ILST):
                        for item_start, item_end in _children(data, ilst_start, ilst_end, _COMMENT):
                            for value_start, value_end in _children(
                                data, item_start, item_end, _DATA
                            ):
                                if value_end - value_start < 8:
                                    raise _Malformed
                                (kind_code,) = struct.unpack_from(">I", data, value_start)
                                if kind_code == _UTF8_DATA:
                                    yield data[value_start + 8 : value_end]


def read_container_marker(content: bytes) -> ContainerMarker:
    """``PRESENT``, ``ABSENT`` o ``UNREADABLE`` para los bytes de un clip MP4."""
    if not isinstance(content, bytes | bytearray | memoryview):
        raise TypeError("content debe ser bytes")
    data = bytes(content)
    try:
        found = any(comment.rstrip(b"\x00") == CONTAINER_MARK for comment in _comments(data))
    except _Malformed:
        return ContainerMarker.UNREADABLE
    return ContainerMarker.PRESENT if found else ContainerMarker.ABSENT
