"""``accreditation.document`` (LC-LAZ-23; TASK-319): el documento portátil legible del paquete.

Reexporta ``renderer.py`` para que el paquete se importe por su nombre de componente
(``vigia_platform.accreditation.document``), igual que ``accreditation.manifest`` o
``accreditation.export``.
"""

from __future__ import annotations

from vigia_platform.accreditation.document.renderer import (
    DOCUMENT_SECTIONS,
    FONT_DIRECTORY,
    FONT_FILES,
    NULL_TEXT,
    DocumentRenderFailed,
    PackagedFontFetcher,
    document_content,
    document_html,
    generate_document,
    render_pdf,
)

__all__ = [
    "DOCUMENT_SECTIONS",
    "FONT_DIRECTORY",
    "FONT_FILES",
    "NULL_TEXT",
    "DocumentRenderFailed",
    "PackagedFontFetcher",
    "document_content",
    "document_html",
    "generate_document",
    "render_pdf",
]
