"""Volúmenes de un paquete exportado y dónde viven (TASK-345; LC-LAZ-22; PAT-LAZ-REN-03;
infraestructura §6.2 y §7.1; NFR-LAZ-10).

- **Claves** bajo el prefijo ``export/`` de ``vigia-evidence``: ``org/{organization_id}/plant/
  {plant_id}/export/{export_id}/vol-{nn}.zip`` (``nn`` de ``01`` a ``99``) y, para la descarga del
  documento portátil (SUP-U05-23), ``.../export/{export_id}/document.pdf``: el documento va
  **dentro** de ``vol-01.zip`` y, además, como objeto propio, porque una URL prefirmada solo
  puede apuntar a un objeto. Las dos caen en ``org/*/export/*``: ningún permiso nuevo (§9.1 y
  §9.2).
- **Tamaños** ``[objetivos propios]``: partes de la subida por partes de **16 MiB**
  (``PART_SIZE``; 20 GB caben en unas 1 250 partes, muy por debajo de las 10 000 del servicio) y
  volúmenes de **2 GB** (``VOLUME_MAX_BYTES``, ``[estimación propia]``), con el **manifiesto en el
  primero**: ``vol-01.zip`` guarda ``MANIFEST_RESERVE_BYTES`` para él, que se escribe al final.
- ``archive_sha256`` del paquete: SHA-256 de ``canonical_bytes`` (RFC 8785, LC-NUC-09; U-04 no
  canonicaliza por su cuenta) de ``{"volumes": [{name, sha256, size_bytes}, …]}`` en orden de
  número. Con un solo volumen no es la SHA-256 del ZIP: es la de esa lista, que un tercero
  recalcula con los volúmenes descargados.
- ``StoredObjects`` es lo que el flujo deja escrito al cerrar (volúmenes y documento, cada uno con
  su ``version_id``), guardado en ``accreditation.export_package.stored_objects``: la descarga
  firma **esa** versión y ninguna otra (como la evidencia de U-02, seguimiento de VIG-65).

Módulo puro: sin E/S, sin FastAPI ni SQLAlchemy y sin hora del sistema.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import JsonValue

from vigia_platform.ledger.canonical import canonical_bytes_sync

__all__ = [
    "DOCUMENT_CONTENT_TYPE",
    "DOCUMENT_NAME",
    "MANIFEST_NAME",
    "MANIFEST_RESERVE_BYTES",
    "MAX_VOLUMES",
    "MIN_PART_SIZE",
    "PART_SIZE",
    "VOLUME_CONTENT_TYPE",
    "VOLUME_MAX_BYTES",
    "ExportLocation",
    "StoredObject",
    "StoredObjects",
    "Volume",
    "archive_sha256",
    "volume_name",
]

PART_SIZE: Final = 16 * 1024 * 1024
"""Tamaño de cada parte de la subida por partes (infraestructura §6.2, ``[objetivo propio]``)."""
MIN_PART_SIZE: Final = 5 * 1024 * 1024
"""Mínimo del servicio para toda parte salvo la última."""
VOLUME_MAX_BYTES: Final = 2 * 1000**3
"""Tamaño máximo de un volumen (``tech-stack-decisions.md`` §3, ``[estimación propia]``)."""
MANIFEST_RESERVE_BYTES: Final = 64 * 1024 * 1024
"""Lo que ``vol-01.zip`` guarda para el manifiesto, que se escribe al cerrar el paquete."""
MAX_VOLUMES: Final = 99
"""``vol-01`` a ``vol-99``: 20 GB estimados caben en 10 volúmenes."""

MANIFEST_NAME: Final = "manifest.json"
DOCUMENT_NAME: Final = "document.pdf"
VOLUME_CONTENT_TYPE: Final = "application/zip"
DOCUMENT_CONTENT_TYPE: Final = "application/pdf"

_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_VERSION_ID: Final = re.compile(r"[\x21-\x7e]{1,1024}")


def volume_name(number: int) -> str:
    """``vol-01.zip`` … ``vol-99.zip``."""
    if type(number) is not int or not 1 <= number <= MAX_VOLUMES:
        raise ValueError(f"el número de volumen va de 1 a {MAX_VOLUMES}")
    return f"vol-{number:02d}.zip"


@dataclass(frozen=True, slots=True)
class ExportLocation:
    """Dónde vive el paquete de una exportación (prefijo ``export/`` de ``vigia-evidence``)."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    export_id: uuid.UUID

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.plant_id, self.export_id):
            if not isinstance(value, uuid.UUID):
                raise TypeError("la ubicación de una exportación son tres UUID")

    @property
    def prefix(self) -> str:
        return f"org/{self.organization_id}/plant/{self.plant_id}/export/{self.export_id}/"

    def volume_key(self, number: int) -> str:
        return self.prefix + volume_name(number)

    @property
    def document_key(self) -> str:
        return self.prefix + DOCUMENT_NAME


def _check_object(size_bytes: object, sha256: object, version_id: object) -> None:
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError("size_bytes debe ser un entero no negativo")
    if not isinstance(sha256, str) or _SHA256_HEX.fullmatch(sha256) is None:
        raise ValueError("sha256 debe ser hexadecimal en minúsculas de 64 caracteres")
    if version_id is not None and (
        not isinstance(version_id, str) or _VERSION_ID.fullmatch(version_id) is None
    ):
        raise ValueError("version_id no válido")


@dataclass(frozen=True, slots=True, kw_only=True)
class Volume:
    """Un volumen escrito: su número, tamaño, SHA-256 de sus bytes y versión del objeto."""

    number: int
    size_bytes: int
    sha256: str
    version_id: str | None

    def __post_init__(self) -> None:
        volume_name(self.number)
        _check_object(self.size_bytes, self.sha256, self.version_id)

    @property
    def name(self) -> str:
        return volume_name(self.number)

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "version_id": self.version_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Volume:
        return cls(
            number=value["number"],
            size_bytes=value["size_bytes"],
            sha256=value["sha256"],
            version_id=value["version_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredObject:
    """Un objeto suelto escrito por el flujo (el documento portátil)."""

    size_bytes: int
    sha256: str
    version_id: str | None

    def __post_init__(self) -> None:
        _check_object(self.size_bytes, self.sha256, self.version_id)

    def to_json(self) -> dict[str, Any]:
        return {"size_bytes": self.size_bytes, "sha256": self.sha256, "version_id": self.version_id}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> StoredObject:
        return cls(
            size_bytes=value["size_bytes"], sha256=value["sha256"], version_id=value["version_id"]
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredObjects:
    """Lo que la descarga sirve de un paquete: sus volúmenes y su documento."""

    volumes: tuple[Volume, ...] = ()
    document: StoredObject | None = None

    def __post_init__(self) -> None:
        numbers = [volume.number for volume in self.volumes]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("los volúmenes van numerados de 1 en adelante, sin huecos")

    def to_json(self) -> dict[str, Any]:
        return {
            "volumes": [volume.to_json() for volume in self.volumes],
            "document": None if self.document is None else self.document.to_json(),
        }

    @classmethod
    def from_json(cls, value: object) -> StoredObjects:
        """La columna ``stored_objects``; ``{}`` (un paquete sin bytes escritos) da vacío."""
        if not isinstance(value, Mapping):
            raise ValueError("stored_objects debe ser un objeto JSON")
        volumes = value.get("volumes") or []
        document = value.get("document")
        return cls(
            volumes=tuple(Volume.from_json(volume) for volume in volumes),
            document=None if document is None else StoredObject.from_json(document),
        )


def archive_sha256(volumes: Sequence[Volume]) -> str:
    """SHA-256 de los bytes canónicos de la lista de volúmenes (nombre, SHA-256 y tamaño)."""
    if not volumes:
        raise ValueError("un paquete tiene al menos un volumen")
    entries: list[JsonValue] = [
        {"name": volume.name, "sha256": volume.sha256, "size_bytes": volume.size_bytes}
        for volume in sorted(volumes, key=lambda volume: volume.number)
    ]
    return hashlib.sha256(canonical_bytes_sync({"volumes": entries})).hexdigest()
