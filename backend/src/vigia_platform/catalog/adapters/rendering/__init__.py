"""Documento legible del acta de comisionamiento: plantilla HTML y CSS a PDF (LC-GOB-08)."""

from vigia_platform.catalog.adapters.rendering.record_document import (
    DOCUMENT_RETRY_AFTER_SECONDS,
    DOCUMENT_TIMEOUT_SECONDS,
    DocumentRenderFailed,
    DocumentTimedOut,
    RecordDocumentRenderer,
)

__all__ = [
    "DOCUMENT_RETRY_AFTER_SECONDS",
    "DOCUMENT_TIMEOUT_SECONDS",
    "DocumentRenderFailed",
    "DocumentTimedOut",
    "RecordDocumentRenderer",
]
