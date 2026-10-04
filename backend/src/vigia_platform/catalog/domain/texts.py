"""Textos obligatorios del catálogo y las compuertas: con contenido, no solo signos.

Un motivo, un alcance o un resumen hechos solo de espacios, puntuación o caracteres invisibles
pasan los límites de longitud pero no dicen nada: un ``reason_es`` así no es un motivo (BR-GOB-33).
Es la misma regla que el motivo de una concesión de U-02 (``identity.application.concessions``):
al menos una letra o un dígito de cualquier escritura.
"""

from __future__ import annotations

import unicodedata

__all__ = ["has_content"]


def has_content(value: str) -> bool:
    """¿Lleva ``value`` al menos una letra o un dígito (categorías Unicode ``L`` o ``N``)?"""
    return any(unicodedata.category(char)[0] in ("L", "N") for char in value)
