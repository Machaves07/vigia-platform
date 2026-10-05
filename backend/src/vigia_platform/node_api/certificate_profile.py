"""Perfil del sujeto del certificado de cliente de un nodo (TASK-206; lo emite TASK-219).

La composición y la lectura viven en ``fleet.domain.node_subject`` (una sola función de cada una,
compartida por la emisión de ``fleet.credentials`` y por la identidad de cada petición): este
módulo las reexporta para ``node_api.identity`` sin repetirlas.
"""

from __future__ import annotations

from vigia_platform.fleet.domain.node_subject import (
    MAX_SERIAL_HEX_CHARS,
    SUBJECT_ATTRIBUTES,
    CertificateProfileError,
    NodeSubject,
    read_subject,
    serial_hex,
    subject_document,
    subject_name,
)

__all__ = [
    "MAX_SERIAL_HEX_CHARS",
    "SUBJECT_ATTRIBUTES",
    "CertificateProfileError",
    "NodeSubject",
    "read_subject",
    "serial_hex",
    "subject_document",
    "subject_name",
]
