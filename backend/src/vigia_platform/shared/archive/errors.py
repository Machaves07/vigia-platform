"""Errores comunes del archivado de particiones (LC-NUC-33; TASK-131, TASK-203).

Los comparten el archivado de la auditoría (``audit_archive``) y el de las tablas de volumen de
``fleet`` (``table_archive``), que corren en la misma tarea ``archive_audit_partitions``.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = ["ArchiveFailure", "ArchiveVerificationFailed", "sqlstate"]


class ArchiveFailure(enum.StrEnum):
    """Motivo por el que un archivo no se da por bueno (``failure_reason`` de la alerta)."""

    SOURCE_BROKEN = "source_broken"
    """La partición ya está rota en la base o su lectura no es la del resumen: no se archiva."""
    UNREADABLE = "unreadable"
    """El archivo no es un ZIP legible, le falta o le sobra un miembro, o no es JSON válido."""
    FORMAT_MISMATCH = "format_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    """Lo leído del almacén no tiene el SHA-256 de lo subido (o el de las filas, el declarado)."""
    VERIFIER_MISMATCH = "verifier_mismatch"
    COUNT_MISMATCH = "count_mismatch"
    CHAIN_BROKEN = "chain_broken"
    ENTRY_MISMATCH = "entry_mismatch"
    """Una entrada o fila, devuelta a columnas, no es la fila de la base."""
    CHECKPOINT_MISMATCH = "checkpoint_mismatch"
    PARTITION_CHANGED = "partition_changed"
    """La base rechazó el desprendimiento: la partición cambió desde la exportación o ya no está
    adjunta."""


class ArchiveVerificationFailed(Exception):
    """El archivo de una partición no pasó la verificación: la partición no se desprende."""

    code: Final = "audit_archive_verification_failed"

    def __init__(self, reason: ArchiveFailure, detail: str = "") -> None:
        super().__init__(f"archivo de auditoría no verificado: {reason.value}")
        self.reason = reason
        self.detail = detail


def sqlstate(error: BaseException) -> str | None:
    """El ``SQLSTATE`` de un error de la base, a través de SQLAlchemy y del adaptador."""
    for candidate in (error, getattr(error, "orig", None), error.__cause__):
        value = getattr(candidate, "sqlstate", None)
        if isinstance(value, str):
            return value
    return None
