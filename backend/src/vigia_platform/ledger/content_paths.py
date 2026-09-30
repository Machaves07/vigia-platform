"""Rutas declaradas sobre un contenido JSON ya validado (``RecordType``; LC-NUC-10).

Las rutas de ``free_text_paths``, ``evidence_paths``, ``source_key_path`` y ``label_rule`` son
punteros JSON con ``[*]`` para los elementos de una lista (``/cameras[*]/clips[*]``). Aquí se
recorren sobre el documento y se devuelve cada valor con su puntero **concreto**
(``/cameras/0/clips/1``), que es el ``field`` de un rechazo. Un campo opcional ausente o ``null``
no produce ningún valor. Módulo puro, sin E/S.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, MutableMapping, MutableSequence, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ContentPathError",
    "Located",
    "contract_field",
    "iter_segments",
    "locate",
    "pointer",
    "replace",
]

_SEGMENT: Final = re.compile(r"/([a-z][a-z0-9_]{0,63})(\[\*\])?")


class ContentPathError(ValueError):
    """La ruta declarada no tiene la forma de ``CONTENT_PATH``."""


@dataclass(frozen=True, slots=True)
class Located:
    """Un valor del documento y el puntero concreto que lleva a él."""

    segments: tuple[str | int, ...]
    value: Any

    @property
    def pointer(self) -> str:
        return pointer(self.segments)


def _parse(path: str) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    position = 0
    for match in _SEGMENT.finditer(path):
        if match.start() != position:
            break
        parts.append((match.group(1), match.group(2) is not None))
        position = match.end()
    if not parts or position != len(path):
        raise ContentPathError(f"ruta declarada mal formada: {path!r}")
    return parts


def locate(document: Any, path: str) -> list[Located]:
    """Cada valor presente (no ``null``) en ``path``, en el orden del documento."""
    found: list[Located] = []
    frontier: list[tuple[tuple[str | int, ...], Any]] = [((), document)]
    for name, each in _parse(path):
        following: list[tuple[tuple[str | int, ...], Any]] = []
        for segments, node in frontier:
            if not isinstance(node, MutableMapping | dict) or node.get(name) is None:
                continue
            child = node[name]
            here = (*segments, name)
            if not each:
                following.append((here, child))
            elif isinstance(child, list):
                following.extend(
                    ((*here, index), item) for index, item in enumerate(child) if item is not None
                )
        frontier = following
    found.extend(Located(segments, value) for segments, value in frontier)
    return found


def replace(document: Any, segments: Sequence[str | int], value: Any) -> None:
    """Sustituye en su sitio el valor de ``segments`` (que ``locate`` devolvió)."""
    node: Any = document
    for segment in segments[:-1]:
        node = node[segment]
    last = segments[-1]
    coherent = (isinstance(node, MutableSequence) and isinstance(last, int)) or (
        isinstance(node, MutableMapping) and isinstance(last, str)
    )
    if coherent:
        node[last] = value
    else:  # pragma: no cover - locate solo devuelve rutas coherentes
        raise ContentPathError("ruta concreta incoherente con el documento")


def _escape(segment: str | int) -> str:
    return str(segment).replace("~", "~0").replace("/", "~1")


def pointer(segments: Sequence[str | int]) -> str:
    """Puntero JSON (RFC 6901) de ``segments``; ``""`` es el documento entero."""
    return "".join(f"/{_escape(segment)}" for segment in segments)


_CONTRACT_FIELD: Final = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,256}$")


def contract_field(segments: Sequence[str | int]) -> str | None:
    """La ruta en la forma del ``RejectionResponse`` del contrato (``cameras[0].clips[1].sha256``).

    ``None`` si queda vacía o no cabe en el patrón del contrato.
    """
    text = ""
    for segment in segments:
        if isinstance(segment, int):
            text += f"[{segment}]"
        else:
            text += f".{segment}" if text else segment
    return text if _CONTRACT_FIELD.fullmatch(text) else None


def iter_segments(pointer_text: str) -> Iterator[str | int]:
    """Segmentos de un puntero producido por ``pointer`` (los números como enteros)."""
    if not pointer_text:
        return
    for raw in pointer_text[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        yield int(token) if token.isdigit() else token
