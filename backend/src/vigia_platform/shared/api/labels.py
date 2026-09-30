"""Etiquetas en español de las listas cerradas de la plataforma (LC-NUC-21; NFR-NUC-51).

``backend/resources/labels.platform.es.json`` es el mapa ``{enumeración: {valor: etiqueta}}`` de
``domain-entities.md`` §1 (y los mensajes de ``api_error_code``). Ningún valor crudo se muestra a
una persona: se muestra su etiqueta.

**Fallo cerrado**: ``label(enumeración, valor)`` lanza ``MissingLabel`` ante un valor sin
etiqueta (nunca devuelve el valor crudo ni una cadena vacía), y ``require_complete`` impide
arrancar si a un miembro de una enumeración del código le falta su etiqueta. ``load`` rechaza un
archivo mal formado: claves que no son ``snake_case``, etiquetas vacías, demasiado largas o con
caracteres de control.
"""

from __future__ import annotations

import enum
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_LABELS_PATH",
    "MAX_LABEL_CHARS",
    "LabelsInvalid",
    "MissingLabel",
    "PlatformLabels",
]

DEFAULT_LABELS_PATH: Final = (
    Path(__file__).resolve().parents[4] / "resources/labels.platform.es.json"
)
"""``backend/resources/labels.platform.es.json`` (se copia a la imagen con ``backend/``)."""
MAX_LABEL_CHARS: Final = 256
_MAX_FILE_BYTES: Final = 256 * 1024
_KEY: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


class LabelsInvalid(Exception):
    """El archivo de etiquetas no existe o no tiene la forma esperada: el proceso no arranca."""


class MissingLabel(LookupError):
    """Un valor no tiene etiqueta: no se muestra (fallo cerrado)."""

    def __init__(self, enumeration: str, value: str) -> None:
        super().__init__(f"sin etiqueta para {enumeration}.{value}")
        self.enumeration = enumeration
        self.value = value


def _valid_label(text: object) -> bool:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_LABEL_CHARS:
        return False
    return not any(unicodedata.category(char).startswith("C") for char in text)


class PlatformLabels:
    """Mapa inmutable de etiquetas por enumeración y valor."""

    def __init__(self, labels: Mapping[str, Mapping[str, str]]) -> None:
        problems: list[str] = []
        table: dict[str, dict[str, str]] = {}
        for enumeration, values in labels.items():
            if not isinstance(enumeration, str) or _KEY.fullmatch(enumeration) is None:
                problems.append(f"enumeración con nombre no válido: {enumeration!r}")
                continue
            if not isinstance(values, Mapping) or not values:
                problems.append(f"la enumeración «{enumeration}» no tiene valores")
                continue
            for value, text in values.items():
                if not isinstance(value, str) or _KEY.fullmatch(value) is None:
                    problems.append(f"valor con nombre no válido en «{enumeration}»: {value!r}")
                elif not _valid_label(text):
                    problems.append(f"etiqueta vacía o no válida en «{enumeration}.{value}»")
            table[enumeration] = dict(values)
        if problems:
            raise LabelsInvalid("; ".join(problems))
        self._labels = table

    @classmethod
    def load(cls, path: Path = DEFAULT_LABELS_PATH) -> PlatformLabels:
        """Lee y valida el archivo; ``LabelsInvalid`` si falta o está mal formado."""
        try:
            raw = path.read_bytes()
        except OSError:
            raise LabelsInvalid("no se pudo leer el archivo de etiquetas") from None
        if len(raw) > _MAX_FILE_BYTES:
            raise LabelsInvalid("el archivo de etiquetas es demasiado grande")
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise LabelsInvalid("el archivo de etiquetas no es JSON válido") from None
        if not isinstance(document, dict):
            raise LabelsInvalid("el archivo de etiquetas debe ser un objeto")
        return cls(document)

    def enumerations(self) -> frozenset[str]:
        return frozenset(self._labels)

    def values(self, enumeration: str) -> frozenset[str]:
        return frozenset(self._labels.get(enumeration, {}))

    def has(self, enumeration: str, value: str) -> bool:
        return value in self._labels.get(enumeration, {})

    def label(self, enumeration: str, value: object) -> str:
        """Etiqueta de ``value`` (``str`` o miembro de ``enum``); ``MissingLabel`` si no hay."""
        raw = value.value if isinstance(value, enum.Enum) else value
        if not isinstance(raw, str):
            raise MissingLabel(enumeration, repr(raw))
        text = self._labels.get(enumeration, {}).get(raw)
        if text is None:
            raise MissingLabel(enumeration, raw)
        return text

    def missing(self, enumeration: str, members: Iterable[object]) -> list[str]:
        """Valores de ``members`` sin etiqueta en ``enumeration``."""
        found: list[str] = []
        for member in members:
            raw = member.value if isinstance(member, enum.Enum) else member
            if not isinstance(raw, str) or not self.has(enumeration, raw):
                found.append(str(raw))
        return found

    def require_complete(self, bindings: Mapping[str, type[enum.Enum]]) -> list[str]:
        """Problemas en español por cada miembro sin etiqueta de las enumeraciones del código."""
        return [
            f"el valor «{value}» de «{enumeration}» no tiene etiqueta en español"
            for enumeration, kind in bindings.items()
            for value in self.missing(enumeration, kind)
        ]


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("clave repetida")
        result[key] = value
    return result
