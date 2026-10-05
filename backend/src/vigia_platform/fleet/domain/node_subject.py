"""Perfil del sujeto del certificado de cliente de un nodo (TASK-206 y TASK-219; BR-GOB-63).

**Una sola función de composición y una de lectura**, compartidas por quien emite el certificado
(``fleet.credentials``, TASK-219) y por quien lo lee en cada petición (``node_api.identity``, que
las importa por ``node_api.certificate_profile``):

- Sujeto: exactamente tres atributos, uno por RDN y en este orden: ``O`` = ``organization_id``,
  ``OU`` = ``plant_id`` y ``CN`` = ``node_id``, cada uno un UUID canónico (minúsculas con guiones).
  Nada más: ni correo, ni nombre de persona, ni huella (P3).
- Número de serie: el entero del certificado en hexadecimal en minúsculas sin ceros a la izquierda
  (``serial_hex``), la forma de ``fleet.node_credential.certificate_serial``.
- ``subject_document``: el objeto ``{node_id, organization_id, plant_id}`` que guarda
  ``node_credential.subject`` (la restricción ``node_credential_subject_matches`` lo exige).

Cualquier otra forma no es un certificado de nodo: ``read_subject`` lanza
``CertificateProfileError`` y la petición se rechaza con ``node_not_enrolled``.

Módulo puro: sin FastAPI, sin SQLAlchemy y sin leer la hora del sistema.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final

from cryptography import x509
from cryptography.x509.oid import NameOID

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

MAX_SERIAL_HEX_CHARS: Final = 64
"""Tope del número de serie en hexadecimal (``node_credential_serial_format``)."""
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUBJECT_ATTRIBUTES: Final = (NameOID.ORGANIZATION_NAME, NameOID.ORGANIZATIONAL_UNIT_NAME,
                             NameOID.COMMON_NAME)  # fmt: skip
"""Los tres atributos del sujeto, en el orden de sus RDN: organización, planta y nodo."""


class CertificateProfileError(ValueError):
    """El certificado no tiene el perfil de un nodo de Vigía."""


@dataclass(frozen=True, slots=True)
class NodeSubject:
    """Lo que el sujeto del certificado afirma: el nodo, su organización y su planta."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID

    def __post_init__(self) -> None:
        for name in ("node_id", "organization_id", "plant_id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")


def subject_name(subject: NodeSubject) -> x509.Name:
    """El ``x509.Name`` del certificado de ``subject`` (``O``, ``OU`` y ``CN``, en ese orden)."""
    if not isinstance(subject, NodeSubject):
        raise TypeError("subject debe ser NodeSubject")
    values = (subject.organization_id, subject.plant_id, subject.node_id)
    return x509.Name(
        [
            x509.NameAttribute(oid, str(value))
            for oid, value in zip(SUBJECT_ATTRIBUTES, values, strict=True)
        ]
    )


def _uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise CertificateProfileError("un atributo del sujeto no es un UUID canónico")
    return uuid.UUID(value)


def read_subject(name: x509.Name) -> NodeSubject:
    """El ``NodeSubject`` de ``name``; ``CertificateProfileError`` si no tiene el perfil exacto."""
    if not isinstance(name, x509.Name):
        raise CertificateProfileError("el sujeto no es un nombre X.509")
    rdns = list(name.rdns)
    if len(rdns) != len(SUBJECT_ATTRIBUTES):
        raise CertificateProfileError("el sujeto no tiene exactamente tres atributos")
    values: list[uuid.UUID] = []
    for rdn, oid in zip(rdns, SUBJECT_ATTRIBUTES, strict=True):
        attributes = list(rdn)
        if len(attributes) != 1 or attributes[0].oid != oid:
            raise CertificateProfileError("el sujeto no tiene los atributos O, OU y CN en orden")
        values.append(_uuid(attributes[0].value))
    organization_id, plant_id, node_id = values
    return NodeSubject(node_id=node_id, organization_id=organization_id, plant_id=plant_id)


def serial_hex(serial: int) -> str:
    """El número de serie como lo guarda ``node_credential``: hexadecimal sin ceros iniciales."""
    if type(serial) is not int or serial <= 0:
        raise CertificateProfileError("el número de serie debe ser un entero positivo")
    text = format(serial, "x")
    if len(text) > MAX_SERIAL_HEX_CHARS:
        raise CertificateProfileError("el número de serie es demasiado largo")
    return text


def subject_document(subject: NodeSubject) -> dict[str, str]:
    """``node_credential.subject``: ``{node_id, organization_id, plant_id}`` como texto."""
    return {
        "node_id": str(subject.node_id),
        "organization_id": str(subject.organization_id),
        "plant_id": str(subject.plant_id),
    }
