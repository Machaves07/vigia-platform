"""Política de texto libre (``FreeTextPolicy``, domain-entities §4.6; LC-NUC-08; BR-NUC-44).

Todo texto libre del expediente vive en una ruta declarada en ``free_text_paths`` de su tipo
(BR-NUC-51) y, antes de calcular el hash, pasa por ``FreeTextPolicyRegistry.apply``:

1. **Política base de U-02** (``apply_base_policy``), que siempre aplica aunque no haya ningún
   validador registrado (fallo cerrado respecto al marcado):

   - normalización Unicode **NFC**: el texto que se guarda es el normalizado;
   - sin caracteres de control (categoría ``Cc``, incluidos tabulador y salto de línea, y los
     separadores de línea y párrafo ``Zl``/``Zp``), sin caracteres de formato invisibles
     (``Cf``: marcas de dirección, anchura cero), sin sustitutos sueltos (``Cs``), de uso
     privado (``Co``) ni sin asignar (``Cn``);
   - sin marcado: ni etiquetas o comentarios (``<b>``, ``</p>``, ``<!--``, ``<?xml``) ni
     secuencias de escape de entidades (``&lt;``, ``&#60;``, ``&#x3C;``); se buscan también en la
     forma NFKC, para que ``<b>`` escrito con signos de ancho completo (U+FF1C, U+FF1E) no pase;
   - longitud en caracteres (tras NFC) entre el mínimo y el máximo que declara el esquema.

2. **Validadores enchufados** (``FreeTextPolicyRegistry.register``), en el orden de registro,
   sobre el texto ya normalizado: U-04 registra al arrancar el de vocabulario bloqueado y
   atribución a personas (RF-PLA-15, C-PLA-28). Un validador acepta o lanza
   ``FreeTextRejected``; no puede reescribir el texto.

El rechazo lo traduce el escritor (TASK-113) a ``free_text_rejected``.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "FreeTextField",
    "FreeTextPolicyRegistry",
    "FreeTextRejected",
    "FreeTextRejection",
    "FreeTextValidator",
    "apply_base_policy",
]

_MARKUP: Final = re.compile(
    r"<[A-Za-z/!?]"  # etiqueta, cierre, comentario o instrucción de procesamiento
    r"|&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);"  # entidad con nombre o numérica
)
_EXPANSION_FACTOR: Final = 4
"""Un texto con más de cuatro veces el máximo de caracteres se rechaza antes de normalizarlo:
ninguna composición NFC reduce tanto, y así una cadena gigante no cuesta una normalización."""

_REJECTED_CATEGORIES: Final = {
    "Cc": "control_character",
    "Zl": "control_character",
    "Zp": "control_character",
    "Cf": "invisible_character",
    "Cs": "disallowed_character",
    "Co": "disallowed_character",
    "Cn": "disallowed_character",
}


class FreeTextRejection(enum.StrEnum):
    """Motivo de rechazo de la política base."""

    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    CONTROL_CHARACTER = "control_character"
    INVISIBLE_CHARACTER = "invisible_character"
    DISALLOWED_CHARACTER = "disallowed_character"
    MARKUP = "markup"


_MESSAGES: Final = {
    FreeTextRejection.TOO_SHORT: "el texto es más corto que el mínimo del campo",
    FreeTextRejection.TOO_LONG: "el texto supera la longitud máxima del campo",
    FreeTextRejection.CONTROL_CHARACTER: "el texto contiene caracteres de control",
    FreeTextRejection.INVISIBLE_CHARACTER: "el texto contiene caracteres invisibles de formato",
    FreeTextRejection.DISALLOWED_CHARACTER: "el texto contiene caracteres no admitidos",
    FreeTextRejection.MARKUP: "el texto contiene marcado o secuencias de escape de marcado",
}


@dataclass(frozen=True)
class FreeTextField:
    """Un campo de texto libre declarado: tipo, ruta y límites de su esquema."""

    record_type: str
    path: str
    min_length: int
    max_length: int

    def __post_init__(self) -> None:
        if self.min_length < 0 or self.max_length < max(self.min_length, 1):
            raise ValueError("límites de longitud incoherentes")


class FreeTextRejected(ValueError):
    """Un texto que no pasa la política; ``reason`` es un código cerrado, sin el texto."""

    def __init__(self, reason: str, field: FreeTextField, message: str) -> None:
        super().__init__(f"{field.record_type} {field.path}: {message}")
        self.reason = reason
        self.field = field


FreeTextValidator = Callable[[str, FreeTextField], None]
"""Validador enchufable: recibe el texto normalizado y lanza ``FreeTextRejected`` si lo rechaza."""


def _reject(reason: FreeTextRejection, field: FreeTextField) -> FreeTextRejected:
    return FreeTextRejected(reason.value, field, _MESSAGES[reason])


def apply_base_policy(text: str, field: FreeTextField) -> str:
    """Devuelve ``text`` en NFC si cumple la política base; si no, ``FreeTextRejected``."""
    if not isinstance(text, str):
        raise TypeError("el texto libre debe ser una cadena")
    if len(text) > _EXPANSION_FACTOR * field.max_length:
        raise _reject(FreeTextRejection.TOO_LONG, field)
    for char in text:
        # Los sustitutos sueltos se miran antes de normalizar: no son texto Unicode válido.
        if "\ud800" <= char <= "\udfff":
            raise _reject(FreeTextRejection.DISALLOWED_CHARACTER, field)
    normalized = unicodedata.normalize("NFC", text)
    for char in normalized:
        reason = _REJECTED_CATEGORIES.get(unicodedata.category(char))
        if reason is not None:
            raise _reject(FreeTextRejection(reason), field)
    if _MARKUP.search(normalized) or _MARKUP.search(unicodedata.normalize("NFKC", normalized)):
        raise _reject(FreeTextRejection.MARKUP, field)
    if len(normalized) > field.max_length:
        raise _reject(FreeTextRejection.TOO_LONG, field)
    if len(normalized) < field.min_length:
        raise _reject(FreeTextRejection.TOO_SHORT, field)
    return normalized


class FreeTextPolicyRegistry:
    """La política base más los validadores que registran las unidades al arrancar."""

    def __init__(self) -> None:
        self._validators: dict[str, FreeTextValidator] = {}
        self._sealed = False

    def register(self, name: str, validator: FreeTextValidator) -> None:
        """Añade un validador (p. ej. el de vocabulario de U-04); solo antes de sellar."""
        if self._sealed:
            raise RuntimeError(
                "la política de texto libre ya está sellada: se registra al arrancar"
            )
        if name in self._validators:
            raise ValueError(f"el validador de texto libre «{name}» ya está registrado")
        self._validators[name] = validator

    def seal(self) -> None:
        """Cierra el registro: después del arranque no se añaden validadores."""
        self._sealed = True

    @property
    def validator_names(self) -> tuple[str, ...]:
        return tuple(self._validators)

    def apply(self, text: str, field: FreeTextField) -> str:
        """Política base y después cada validador; devuelve el texto NFC que se guarda."""
        normalized = apply_base_policy(text, field)
        for validator in self._validators.values():
            validator(normalized, field)
        return normalized
