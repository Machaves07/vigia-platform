"""Esquemas de contenido de los tipos de registro de U-02 (LC-NUC-08).

``register_u02_record_types`` los registra al arrancar; los de U-03 y U-04 los registran esas
unidades con el mismo ``RecordTypeRegistry``.
"""

from __future__ import annotations

from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry

__all__ = ["U02_RECORD_TYPES", "register_u02_record_types"]


def register_u02_record_types(registry: RecordTypeRegistry) -> None:
    """Registra los catorce tipos de U-02; ``RecordTypeRejected`` si alguno no cumple."""
    for definition in U02_RECORD_TYPES:
        registry.register(definition)
