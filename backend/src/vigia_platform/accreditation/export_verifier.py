"""Verificador endurecido del paquete exportado de U-04 (LC-LAZ-25; PAT-LAZ-SEG-02; BR-LAZ-90 y 91;
NFR-LAZ-23 y 53; PR-LAZ-28; escenario L-6).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10, porque
``tools/build_verifier.py`` lo copia, detrás de los cuatro módulos puros del verificador de U-02
(``pure_rfc8785``, ``pure_ed25519``, ``chain_walk`` y ``package_verifier``), en el archivo único
``resources/verifier/vigia_verify.py`` que viaja dentro de cada paquete (``accreditation.
verifier_bundle``). U-04 **no rediseña** el verificador de U-02: reutiliza su lectura estricta de
JSON, su canonicalización RFC 8785, su verificación Ed25519 y el mensaje firmado de los puntos de
control, y le añade la lectura del paquete exportado con estas defensas, en este orden:

1. **Volúmenes**: ``vol-01.zip`` … ``vol-99.zip`` seguidos, ninguno enlace simbólico. Antes de
   que ``zipfile`` lea el directorio central se lee el registro final del ZIP: sin ZIP64 (el
   paquete nunca lo usa), en un solo disco y con **un número de entradas acotado**
   (``max_volume_entries`` por volumen y ``max_entries`` en total).
2. **Cada entrada, sin leer sus datos**: ruta con ``..`` (``path_traversal``), absoluta o con
   letra de unidad (``absolute_path``), o fuera del patrón cerrado de rutas del paquete
   (``path_not_allowed``); **enlace simbólico** (``symlink``) u otro archivo especial
   (``special_file``); cifrada o con un método distinto de ``stored`` y ``deflate``
   (``compression_not_allowed``); tamaño descomprimido de más de **100 veces** el comprimido
   (``expansion_ratio``) o de más de **2 GB** (``entry_too_large``) ``[objetivos propios]``;
   repetida, también sin distinguir mayúsculas (``duplicate_entry``); un archivo que a la vez es
   directorio de otro (``path_not_allowed``).
3. **Manifiesto** (``manifest.json`` de ``vol-01``): JSON estricto y la forma que se usa; con
   ``--manifest-sha256``, su SHA-256 debe ser el que la plataforma registró al producirlo
   (``export_produced``).
4. **Ancla del manifiesto** (decisión del dueño A2 y B1, 2026-10-19): ``manifest.signature.json``
   de ``vol-01`` lleva la firma Ed25519, con la clave ``checkpoint`` de los puntos de control, de
   ``{kind, export_id, organization_id, plant_id, manifest_sha256, generated_at}``. Se comprueba
   con las claves públicas incluidas (leídas y hasheadas antes que el resto), su vigencia en
   ``generated_at`` y que el SHA-256 firmado es el del manifiesto leído
   (``manifest_signature_mismatch``). Como el manifiesto lista el hash de cada artefacto, la firma
   ata al expediente todo lo que el paquete lleva. **Sin firma válida y sin ``--manifest-sha256``
   el resultado nunca es ``intact``**: es ``unanchored`` (código 3), y no se extrae nada.
5. **Hashes del manifiesto antes de extraer nada**: cada artefacto existe en su volumen con su
   tamaño y nada sobra; después se lee cada uno **en flujo**, sin escribir en disco, y su SHA-256
   debe ser el declarado.
6. **Contenido**: claves públicas y puntos de control iguales en el manifiesto y en sus archivos;
   cada punto de control con forma y cobertura exactas, clave conocida, **vigente al firmar**
   (una clave retirada no firma después de su retiro) y firma Ed25519 válida; el
   ``content_hash`` de cada registro con contenido; el ``entry_hash`` de cada entrada de
   auditoría, recalculado entero con el sobre de U-02; los enlaces entre registros y entradas
   consecutivos y con lo que cubre cada punto de control; y los conteos por estado, los clips,
   los adjuntos y las entradas ausentes del manifiesto frente a lo que el paquete lleva.
7. Solo si todo lo anterior está íntegro **y anclado** y se pide ``--extract``, se escriben los
   archivos en un directorio vacío, volviendo a comprobar cada hash mientras se escriben.

Sin el ancla, las comprobaciones 5 y 6 solo prueban la coherencia interna: el paquete exporta cada
registro sin su actor ni su alcance (no se recalcula su ``record_hash``) y solo una parte de la
cadena, así que quien re-hashea el manifiesto puede cambiar el contenido o el estado de un
hallazgo de forma coherente. Por eso sin ancla el resultado es ``unanchored``. La firma se
comprueba con las claves que viajan en el paquete: quien desconfíe de ellas las compara con las
que publica la plataforma (``/.well-known/vigia-checkpoint-keys``).

Resultado: ``intact`` (código 0), ``broken`` (código 1) con la **posición** del primer fallo
(volumen, artefacto, desplazamiento de su cabecera en el volumen y puntero JSON dentro del
artefacto) o ``unanchored`` (código 3); 2 si la orden es incorrecta. Sin red, sin hora del sistema
y sin secretos.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import shutil
import stat
import struct
import sys
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final, TypeGuard

from vigia_platform.ledger.chain.chain_walk import checkpoint_message
from vigia_platform.ledger.chain.package_verifier import (
    EXIT_BROKEN,
    EXIT_INTACT,
    EXIT_USAGE,
    _Formatter,
    parse_json,
)
from vigia_platform.ledger.chain.pure_ed25519 import ed25519_verify
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError, canonicalize

EXPORT_VERIFIER_VERSION: Final = "export-verifier.1"
"""Versión de este verificador: la que el manifiesto declara en ``verifier_version``."""

EXPORT_MANIFEST_VERSION: Final = 1
EXPORT_MANIFEST_NAME: Final = "manifest.json"
EXPORT_VERIFIER_NAME: Final = "vigia_verify.py"
EXPORT_VERIFIER_ABSENT: Final = "absent"
EXPORT_SIGNATURE_NAME: Final = "manifest.signature.json"
"""La firma del SHA-256 del manifiesto, en ``vol-01`` junto a él (no se lista como artefacto)."""
MANIFEST_SIGNATURE_KIND: Final = "vigia.export.manifest"
"""``kind`` del mensaje firmado: separa esta firma de la de un punto de control."""
MANIFEST_SIGNATURE_PAYLOAD_KEYS: Final = frozenset(
    {"export_id", "generated_at", "kind", "manifest_sha256", "organization_id", "plant_id"}
)
MANIFEST_SIGNATURE_DECLARATION: Final = {
    "path": EXPORT_SIGNATURE_NAME,
    "algorithm": "Ed25519",
    "key_purpose": "checkpoint",
    "payload_kind": MANIFEST_SIGNATURE_KIND,
}
"""Lo que el manifiesto declara en ``manifest_signature`` cuando lleva firma."""
EXIT_UNANCHORED: Final = 3
"""Sin firma válida del manifiesto ni ``--manifest-sha256``: la integridad no está anclada."""
MAX_SIGNATURE_BYTES: Final = 64 * 1024

MAX_EXPANSION_RATIO: Final = 100
"""Una entrada no se descomprime a más de 100 veces su tamaño comprimido ``[objetivo propio]``."""
MAX_ENTRY_BYTES: Final = 2 * 1000**3
"""Ninguna entrada pasa de 2 GB descomprimida ``[objetivo propio]`` (el tamaño de un volumen)."""
MAX_VOLUME_ENTRIES: Final = 65_535
"""Lo más que cabe en un ZIP sin ZIP64."""
MAX_ENTRIES: Final = 1_000_000
"""Entradas de todo el paquete ``[objetivo propio]``."""
MAX_VOLUMES: Final = 99
MAX_EXPORT_MANIFEST_BYTES: Final = 64 * 1024 * 1024
"""Lo que ``vol-01`` reserva para el manifiesto."""
MAX_JSON_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
"""Tope de un artefacto JSON que se interpreta además de hashearse ``[objetivo propio]``."""
READ_CHUNK: Final = 1024 * 1024

_VOLUME_NAME: Final = re.compile(r"vol-([0-9]{2})\.zip")
_ENTRY_PATH: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_MAX_PATH: Final = 255
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_UUID_FORM: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256_FORM: Final = re.compile(r"[0-9a-f]{64}")
_STAMP_FORM: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SIGNATURE_FORM: Final = re.compile(r"[A-Za-z0-9+/]{86}==")
_PUBLIC_KEY_FORM: Final = re.compile(r"[A-Za-z0-9+/]{43}=")
_KEY_ID_FORM: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_END_OF_DIRECTORY: Final = b"PK\x05\x06"
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_END_OF_DIRECTORY_SIZE: Final = 22
_MAX_COMMENT: Final = 65_535
_ALLOWED_METHODS: Final = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

JSON_KINDS: Final = ("findings_json", "audit", "checkpoint", "public_key")
"""Artefactos que, además de hashearse, se interpretan."""
OPEN_LIFECYCLE_STATES: Final = ("received", "in_review", "open", "action_assigned", "overdue")
"""Los estados que el manifiesto cuenta como abiertos (``accreditation.manifest.build``)."""
KEY_STATUSES: Final = ("active", "overlapping", "retired")
CHECKPOINT_CONTENT_KEYS: Final = frozenset(
    {"covered_hash", "covered_sequence", "key_id", "signature", "taken_at"}
)
_ARTIFACT_KEYS: Final = frozenset({"kind", "path", "sha256", "size_bytes", "volume"})
_KEY_KEYS_EXPORT: Final = frozenset(
    {"algorithm", "key_id", "public_key", "status", "valid_from", "valid_until"}
)
_CHECKPOINT_ENTRY_KEYS: Final = frozenset(
    {"content", "entry_hash", "entry_id", "kind", "plant_id", "sequence"}
)
_AUDIT_EXPORT_KEYS: Final = frozenset(
    {
        "actor",
        "chain_sequence",
        "correlation_id",
        "entry_hash",
        "entry_id",
        "filters",
        "occurred_at",
        "operation",
        "outcome",
        "previous_hash",
        "resource_id",
        "resource_kind",
        "result_count",
        "scope_plant_id",
        "scope_zone_id",
    }
)
_AUDIT_ACTOR_KEYS: Final = frozenset(
    {"concession_id", "display_name_snapshot", "id", "kind", "role_in_use", "unit"}
)

EXPORT_MESSAGES: Final = {
    "package_unreadable": "el paquete no se puede leer",
    "too_many_entries": "el paquete tiene más entradas de las permitidas",
    "zip64_not_allowed": "el volumen usa ZIP64 o varios discos: el paquete nunca los usa",
    "path_traversal": "una ruta sube de directorio con «..»",
    "absolute_path": "una ruta es absoluta",
    "path_not_allowed": "una ruta no tiene la forma de las rutas del paquete",
    "symlink": "una entrada es un enlace simbólico",
    "special_file": "una entrada no es un archivo regular",
    "duplicate_entry": "una entrada está repetida",
    "compression_not_allowed": "una entrada está cifrada o usa un método de compresión no admitido",
    "expansion_ratio": "una entrada se expande más de 100 veces su tamaño comprimido",
    "entry_too_large": "una entrada supera los 2 GB descomprimida",
    "manifest_missing": "falta el manifiesto en vol-01.zip",
    "manifest_invalid": "el manifiesto no es válido",
    "manifest_hash_mismatch": "el SHA-256 del manifiesto no es el registrado por la plataforma",
    "artifact_missing": "falta un artefacto que el manifiesto declara",
    "artifact_size_mismatch": "el tamaño de un artefacto no es el que declara el manifiesto",
    "unlisted_entry": "el paquete lleva una entrada que el manifiesto no declara",
    "artifact_unreadable": "un artefacto no se puede descomprimir íntegro",
    "artifact_hash_mismatch": "el SHA-256 de un artefacto no es el que declara el manifiesto",
    "artifact_invalid": "un artefacto no tiene la forma esperada",
    "manifest_mismatch": "el manifiesto no coincide con lo que el paquete lleva",
    "key_invalid": "una clave pública no es válida",
    "checkpoint_invalid": "un punto de control no tiene la forma esperada",
    "unknown_key": "la clave de un punto de control no está entre las claves públicas incluidas",
    "key_not_valid_at_signing": "la clave no estaba vigente cuando se tomó el punto de control",
    "bad_signature": "la firma de un punto de control no verifica",
    "checkpoint_mismatch": "un registro no coincide con lo que cubre un punto de control firmado",
    "content_not_canonical": "un contenido no tiene forma canónica RFC 8785",
    "content_hash_mismatch": "el hash del contenido de un registro no coincide",
    "entry_hash_mismatch": "el hash de una entrada de auditoría no coincide con su sobre",
    "previous_hash_mismatch": "el enlace con el registro anterior no coincide",
    "record_conflict": "dos registros distintos ocupan la misma secuencia de la cadena",
    "extract_failed": "no se pudo extraer un archivo tal como se verificó",
    "manifest_signature_invalid": "la firma del manifiesto no tiene la forma esperada o falta",
    "manifest_signature_mismatch": "la firma del manifiesto no corresponde a este manifiesto",
}
"""Motivo de rotura → explicación en español (lista cerrada)."""


# --- Resultado ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportPosition:
    """Dónde está el primer fallo: volumen, artefacto, desplazamiento y puntero JSON."""

    volume: str | None = None
    artifact: str | None = None
    offset: int | None = None
    """Byte del volumen donde empieza la cabecera local del artefacto."""
    pointer: str | None = None
    """Puntero JSON (RFC 6901) dentro del artefacto."""


@dataclass(frozen=True)
class ExportFailure:
    reason: str
    position: ExportPosition
    detail: str = ""

    def message(self) -> str:
        text = EXPORT_MESSAGES.get(self.reason, self.reason)
        return f"{text} ({self.detail})" if self.detail else text


class ExportBroken(Exception):
    """El paquete está roto: lleva el primer fallo."""

    def __init__(self, failure: ExportFailure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


@dataclass(frozen=True)
class ExportLimits:
    """Topes del verificador; los de producción son los del diseño."""

    max_expansion_ratio: int = MAX_EXPANSION_RATIO
    max_entry_bytes: int = MAX_ENTRY_BYTES
    max_volume_entries: int = MAX_VOLUME_ENTRIES
    max_entries: int = MAX_ENTRIES
    max_manifest_bytes: int = MAX_EXPORT_MANIFEST_BYTES
    max_json_bytes: int = MAX_JSON_ARTIFACT_BYTES


@dataclass
class ExportReport:
    """Resultado de verificar un paquete exportado."""

    package: str
    failure: ExportFailure | None = None
    volumes: list[str] = field(default_factory=list)
    manifest_sha256: str | None = None
    declared_verifier_version: str | None = None
    artifacts_verified: int = 0
    checkpoints_verified: int = 0
    records_checked: int = 0
    audit_entries_checked: int = 0
    extracted_to: str | None = None
    anchored_by: list[str] = field(default_factory=list)
    """``manifest_signature`` y/o ``manifest_sha256``: lo que ata el manifiesto a la plataforma."""

    @property
    def intact(self) -> bool:
        """Íntegro **y anclado**: sin ancla nunca es ``intact`` (decisión B1)."""
        return self.failure is None and bool(self.anchored_by)

    @property
    def status(self) -> str:
        if self.failure is not None:
            return "broken"
        return "intact" if self.anchored_by else "unanchored"


def _fail(
    reason: str,
    detail: str = "",
    *,
    volume: str | None = None,
    artifact: str | None = None,
    offset: int | None = None,
    pointer: str | None = None,
) -> ExportBroken:
    return ExportBroken(
        ExportFailure(reason, ExportPosition(volume, artifact, offset, pointer), detail)
    )


# --- Volúmenes y entradas -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    volume: str
    info: zipfile.ZipInfo

    @property
    def name(self) -> str:
        return self.info.filename

    @property
    def offset(self) -> int:
        return self.info.header_offset


def find_volumes(path: Path) -> list[Path]:
    """Los volúmenes del paquete: un directorio con ``vol-NN.zip`` o la ruta de ``vol-01.zip``.

    Cualquier otro ``.zip`` se lee como paquete de un solo volumen.
    """
    if path.is_symlink():
        raise _fail("symlink", "el paquete es un enlace simbólico", volume=path.name)
    if path.is_dir():
        directory = path
        names = sorted(child.name for child in path.iterdir() if _VOLUME_NAME.fullmatch(child.name))
    elif path.is_file():
        if _VOLUME_NAME.fullmatch(path.name) is None:
            return [path]
        if path.name != "vol-01.zip":
            raise _fail("package_unreadable", "indique vol-01.zip o el directorio de los volúmenes")
        directory = path.parent
        names = sorted(
            child.name for child in directory.iterdir() if _VOLUME_NAME.fullmatch(child.name)
        )
    else:
        raise _fail("package_unreadable", "no es un directorio ni un archivo")
    if not names:
        raise _fail("package_unreadable", "no hay ningún vol-NN.zip")
    expected = [f"vol-{number:02d}.zip" for number in range(1, len(names) + 1)]
    if names != expected or len(names) > MAX_VOLUMES:
        raise _fail(
            "package_unreadable", "los volúmenes no van de vol-01.zip en adelante sin huecos"
        )
    volumes = [directory / name for name in names]
    for volume in volumes:
        if volume.is_symlink():
            raise _fail("symlink", "el volumen es un enlace simbólico", volume=volume.name)
        if not volume.is_file():
            raise _fail("package_unreadable", "el volumen no es un archivo", volume=volume.name)
    return volumes


def _end_of_directory_entries(handle: IO[bytes], volume: str) -> int:
    """Entradas que declara el registro final del ZIP, leído antes que el directorio central."""
    size = handle.seek(0, 2)
    tail_size = min(size, _END_OF_DIRECTORY_SIZE + _MAX_COMMENT)
    handle.seek(size - tail_size)
    tail = handle.read(tail_size)
    index = tail.rfind(_END_OF_DIRECTORY)
    if index < 0 or len(tail) - index < _END_OF_DIRECTORY_SIZE:
        raise _fail("package_unreadable", "no es un ZIP", volume=volume)
    disk, disk_with_directory, on_disk, total, directory_size, directory_offset = struct.unpack(
        "<4H2L", tail[index + 4 : index + 20]
    )
    zip64 = index >= 20 and tail[index - 20 : index - 16] == _ZIP64_LOCATOR
    if (
        zip64
        or disk != 0
        or disk_with_directory != 0
        or on_disk != total
        or total == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise _fail("zip64_not_allowed", volume=volume)
    return int(total)


def check_entry_name(entry: _Entry) -> None:
    """Rechaza ``..``, rutas absolutas y todo lo que no sea una ruta relativa del paquete."""
    raw = entry.info.orig_filename
    name = entry.name
    where: dict[str, Any] = {"volume": entry.volume, "artifact": raw, "offset": entry.offset}
    parts = raw.replace("\\", "/").split("/")
    if ".." in parts:
        raise _fail("path_traversal", **where)
    if raw.startswith(("/", "\\")) or _DRIVE.match(raw) is not None:
        raise _fail("absolute_path", **where)
    if raw != name or len(raw) > _MAX_PATH or _ENTRY_PATH.fullmatch(raw) is None:
        raise _fail("path_not_allowed", **where)


def check_entry_type(entry: _Entry) -> None:
    """Rechaza enlaces simbólicos y cualquier entrada que no sea un archivo regular."""
    mode = entry.info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFLNK:
        raise _fail("symlink", volume=entry.volume, artifact=entry.name, offset=entry.offset)
    if kind not in (0, stat.S_IFREG) or entry.info.is_dir():
        raise _fail("special_file", volume=entry.volume, artifact=entry.name, offset=entry.offset)


def check_entry_expansion(entry: _Entry, limits: ExportLimits) -> None:
    """Rechaza métodos no admitidos y expansiones de más de 100 veces o de más de 2 GB."""
    info = entry.info
    where: dict[str, Any] = {"volume": entry.volume, "artifact": entry.name, "offset": entry.offset}
    if info.flag_bits & 0x1 or info.compress_type not in _ALLOWED_METHODS:
        raise _fail("compression_not_allowed", **where)
    if info.file_size > limits.max_entry_bytes:
        raise _fail("entry_too_large", f"{info.file_size} bytes", **where)
    if info.compress_type == zipfile.ZIP_STORED and info.file_size != info.compress_size:
        raise _fail("expansion_ratio", "entrada sin comprimir con tamaños distintos", **where)
    if info.file_size > limits.max_expansion_ratio * info.compress_size:
        raise _fail(
            "expansion_ratio",
            f"{info.file_size} bytes de {info.compress_size} comprimidos",
            **where,
        )


class _Package:
    """Los volúmenes abiertos y sus entradas, ya comprobadas sin leer ningún dato."""

    def __init__(self, volumes: Sequence[Path], limits: ExportLimits) -> None:
        self.limits = limits
        self.archives: dict[str, zipfile.ZipFile] = {}
        self.entries: dict[str, _Entry] = {}
        self._files: set[str] = set()
        self._directories: set[str] = set()
        try:
            self._open(volumes)
        except BaseException:
            self.close()
            raise

    def _open(self, volumes: Sequence[Path]) -> None:
        total = 0
        for number, path in enumerate(volumes, start=1):
            # El nombre lógico del volumen (un paquete de un volumen puede llamarse de otro modo).
            volume = f"vol-{number:02d}.zip"
            try:
                handle = path.open("rb")
            except OSError as error:
                raise _fail("package_unreadable", str(error), volume=volume) from None
            try:
                declared = _end_of_directory_entries(handle, volume)
            except BaseException:
                handle.close()
                raise
            total += declared
            if declared > self.limits.max_volume_entries or total > self.limits.max_entries:
                handle.close()
                raise _fail("too_many_entries", f"{total} entradas", volume=volume)
            try:
                archive = zipfile.ZipFile(handle)
            except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
                handle.close()
                raise _fail("package_unreadable", str(error), volume=volume) from None
            self.archives[volume] = archive
            infos = archive.infolist()
            if len(infos) != declared:
                raise _fail("package_unreadable", "el directorio central no cuadra", volume=volume)
            for info in infos:
                entry = _Entry(volume, info)
                check_entry_name(entry)
                check_entry_type(entry)
                check_entry_expansion(entry, self.limits)
                self._check_collisions(entry)
                self.entries[entry.name] = entry

    def _check_collisions(self, entry: _Entry) -> None:
        """Dos rutas que al extraer serían el mismo archivo (también en sistemas de archivos que
        no distinguen mayúsculas), o un archivo que a la vez es directorio de otro."""
        where: dict[str, Any] = {
            "volume": entry.volume,
            "artifact": entry.name,
            "offset": entry.offset,
        }
        folded = entry.name.casefold()
        if folded in self._files:
            raise _fail("duplicate_entry", **where)
        parts = folded.split("/")
        prefixes = ["/".join(parts[:index]) for index in range(1, len(parts))]
        if folded in self._directories or any(prefix in self._files for prefix in prefixes):
            raise _fail("path_not_allowed", "un archivo es a la vez directorio de otro", **where)
        self._files.add(folded)
        self._directories.update(prefixes)

    def stream(self, entry: _Entry) -> Iterator[bytes]:
        """Los bytes descomprimidos de ``entry``, en trozos; nunca más de los declarados."""
        where: dict[str, Any] = {
            "volume": entry.volume,
            "artifact": entry.name,
            "offset": entry.offset,
        }
        archive = self.archives[entry.volume]
        read = 0
        try:
            with archive.open(entry.info) as stream:
                while True:
                    chunk = stream.read(READ_CHUNK)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > entry.info.file_size:
                        raise _fail("artifact_unreadable", "más bytes de los declarados", **where)
                    yield chunk
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError) as error:
            raise _fail("artifact_unreadable", str(error), **where) from None
        if read != entry.info.file_size:
            raise _fail("artifact_unreadable", "menos bytes de los declarados", **where)

    def close(self) -> None:
        for archive in self.archives.values():
            fp = archive.fp
            archive.close()
            if fp is not None:
                fp.close()


# --- Manifiesto ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Artifact:
    path: str
    kind: str
    sha256: str
    size_bytes: int
    volume: int
    pointer: str


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _manifest_error(detail: str, pointer: str) -> ExportBroken:
    return _fail(
        "manifest_invalid",
        detail,
        volume="vol-01.zip",
        artifact=EXPORT_MANIFEST_NAME,
        pointer=pointer,
    )


def _require(document: Mapping[str, Any], key: str, check: Any, pointer: str = "") -> Any:
    value = document.get(key)
    if key not in document or not check(value):
        raise _manifest_error(f"{key} no es válido", f"{pointer}/{key}")
    return value


def _artifacts(items: object) -> list[_Artifact]:
    if not isinstance(items, list):
        raise _manifest_error("artifacts no es una lista", "/artifacts")
    artifacts: list[_Artifact] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        pointer = f"/artifacts/{index}"
        if not isinstance(item, dict) or item.keys() != _ARTIFACT_KEYS:
            raise _manifest_error("un artefacto no tiene las claves esperadas", pointer)
        path, kind, sha256 = item["path"], item["kind"], item["sha256"]
        size, volume = item["size_bytes"], item["volume"]
        if (
            not _matches(_ENTRY_PATH, path)
            or len(path) > _MAX_PATH
            or path in (EXPORT_MANIFEST_NAME, EXPORT_SIGNATURE_NAME)
            or path in seen
        ):
            raise _manifest_error("ruta de artefacto no válida o repetida", pointer + "/path")
        if not isinstance(kind, str) or not kind:
            raise _manifest_error("kind no es válido", pointer + "/kind")
        if not _matches(_SHA256_FORM, sha256):
            raise _manifest_error("sha256 no es válido", pointer + "/sha256")
        if not _is_count(size):
            raise _manifest_error("size_bytes no es válido", pointer + "/size_bytes")
        if not _is_count(volume) or not 1 <= volume <= MAX_VOLUMES:
            raise _manifest_error("volume no es válido", pointer + "/volume")
        seen.add(path)
        artifacts.append(_Artifact(path, kind, sha256, size, volume, pointer))
    return artifacts


@dataclass(frozen=True)
class _Manifest:
    document: dict[str, Any]
    organization_id: str
    plant_id: str
    artifacts: list[_Artifact]
    verifier_version: str


def _read_manifest(package: _Package, expected_sha256: str | None) -> tuple[_Manifest, str]:
    entry = package.entries.get(EXPORT_MANIFEST_NAME)
    if entry is None or entry.volume != "vol-01.zip":
        raise _fail("manifest_missing", volume="vol-01.zip", artifact=EXPORT_MANIFEST_NAME)
    if entry.info.file_size > package.limits.max_manifest_bytes:
        raise _fail(
            "manifest_invalid",
            "el manifiesto supera su tope",
            volume=entry.volume,
            artifact=entry.name,
            offset=entry.offset,
        )
    data = b"".join(package.stream(entry))
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise _fail(
            "manifest_hash_mismatch", volume=entry.volume, artifact=entry.name, offset=entry.offset
        )
    try:
        document = parse_json(data)
    except ValueError as error:
        raise _manifest_error(str(error), "") from None
    if not isinstance(document, dict):
        raise _manifest_error("no es un objeto JSON", "")
    version = document.get("manifest_version")
    if not _is_count(version) or version != EXPORT_MANIFEST_VERSION:
        raise _manifest_error(
            f"manifest_version {version!r}: use el verificador incluido en el paquete",
            "/manifest_version",
        )
    organization = _require(document, "organization", lambda value: isinstance(value, dict))
    organization_id = _require(
        organization, "organization_id", lambda value: _matches(_UUID_FORM, value), "/organization"
    )
    plant = _require(document, "plant", lambda value: isinstance(value, dict))
    plant_id = _require(plant, "plant_id", lambda value: _matches(_UUID_FORM, value), "/plant")
    verifier_version = _require(document, "verifier_version", lambda value: isinstance(value, str))
    for key in ("checkpoints", "checkpoint_public_keys", "zones", "clips", "attachments"):
        _require(document, key, lambda value: isinstance(value, list))
    _require(document, "absent_entries", lambda value: isinstance(value, list))
    manifest = _Manifest(
        document, organization_id, plant_id, _artifacts(document.get("artifacts")), verifier_version
    )
    return manifest, digest


# --- Hashes antes de extraer --------------------------------------------------------------------


def check_artifact_hash(entry: _Entry, artifact: _Artifact, digest: str) -> None:
    """El SHA-256 de los bytes leídos es el que declara el manifiesto."""
    if digest != artifact.sha256:
        raise _fail(
            "artifact_hash_mismatch",
            volume=entry.volume,
            artifact=artifact.path,
            offset=entry.offset,
            pointer=artifact.pointer,
        )


def verify_artifact_hashes(
    package: _Package, manifest: _Manifest, report: ExportReport
) -> dict[str, Any]:
    """Cada artefacto existe con su tamaño y su SHA-256, y nada sobra; devuelve los JSON leídos."""
    listed = {EXPORT_MANIFEST_NAME}
    signature = package.entries.get(EXPORT_SIGNATURE_NAME)
    if signature is not None and signature.volume == "vol-01.zip":
        listed.add(EXPORT_SIGNATURE_NAME)
    for artifact in manifest.artifacts:
        volume = f"vol-{artifact.volume:02d}.zip"
        entry = package.entries.get(artifact.path)
        if entry is None or entry.volume != volume:
            raise _fail(
                "artifact_missing",
                "no está en su volumen",
                volume=volume,
                artifact=artifact.path,
                pointer=artifact.pointer,
            )
        if entry.info.file_size != artifact.size_bytes:
            raise _fail(
                "artifact_size_mismatch",
                volume=volume,
                artifact=artifact.path,
                offset=entry.offset,
                pointer=artifact.pointer,
            )
        listed.add(artifact.path)
    for name, entry in sorted(
        package.entries.items(), key=lambda item: (item[1].volume, item[1].offset)
    ):
        if name not in listed:
            raise _fail("unlisted_entry", volume=entry.volume, artifact=name, offset=entry.offset)
    documents: dict[str, Any] = {}
    for artifact in manifest.artifacts:
        entry = package.entries[artifact.path]
        keep = artifact.kind in JSON_KINDS and artifact.size_bytes <= package.limits.max_json_bytes
        hasher = hashlib.sha256()
        kept: list[bytes] = []
        for chunk in package.stream(entry):
            hasher.update(chunk)
            if keep:
                kept.append(chunk)
        check_artifact_hash(entry, artifact, hasher.hexdigest())
        report.artifacts_verified += 1
        if keep:
            try:
                documents[artifact.path] = parse_json(b"".join(kept))
            except ValueError as error:
                raise _fail(
                    "artifact_invalid",
                    str(error),
                    volume=entry.volume,
                    artifact=artifact.path,
                    offset=entry.offset,
                ) from None
    return documents


# --- Ancla del manifiesto -----------------------------------------------------------------------


def _parse_key_items(
    items: object, where: Callable[[str], dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Las claves públicas ``checkpoint`` incluidas, validadas, por ``key_id``."""
    if not isinstance(items, list):
        raise _fail("artifact_invalid", "keys no es una lista", **where("/keys"))
    keys: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        position = where(f"/keys/{index}")
        if not isinstance(item, dict) or item.keys() != _KEY_KEYS_EXPORT:
            raise _fail("key_invalid", "claves inesperadas", **position)
        key_id, text = item["key_id"], item["public_key"]
        if not _matches(_KEY_ID_FORM, key_id) or key_id in keys:
            raise _fail("key_invalid", "key_id no válido o repetido", **position)
        if item["algorithm"] != "Ed25519" or item["status"] not in KEY_STATUSES:
            raise _fail("key_invalid", "algoritmo o estado no válido", **position)
        valid_from, valid_until = item["valid_from"], item["valid_until"]
        if not (
            _matches(_STAMP_FORM, valid_from)
            and _matches(_STAMP_FORM, valid_until)
            and valid_from < valid_until
        ):
            raise _fail("key_invalid", "vigencia no válida", **position)
        raw = _canonical_base64(text, _PUBLIC_KEY_FORM)
        if raw is None or len(raw) != 32:
            raise _fail("key_invalid", "clave pública no válida", **position)
        keys[key_id] = {"raw": raw, "valid_from": valid_from, "valid_until": valid_until}
    return keys


def _included_keys(package: _Package, manifest: _Manifest) -> dict[str, dict[str, Any]]:
    """Las claves del artefacto ``public_key``, leído y hasheado antes que el resto."""
    found = [artifact for artifact in manifest.artifacts if artifact.kind == "public_key"]
    if len(found) != 1:
        raise _fail(
            "manifest_signature_invalid",
            "sin un artefacto de claves públicas no se puede comprobar la firma",
            volume="vol-01.zip",
            artifact=EXPORT_SIGNATURE_NAME,
        )
    artifact = found[0]
    entry = package.entries.get(artifact.path)
    volume = f"vol-{artifact.volume:02d}.zip"
    if entry is None or entry.volume != volume:
        raise _fail("artifact_missing", volume=volume, artifact=artifact.path)
    if entry.info.file_size != artifact.size_bytes or (
        artifact.size_bytes > package.limits.max_json_bytes
    ):
        raise _fail(
            "artifact_size_mismatch", volume=volume, artifact=artifact.path, offset=entry.offset
        )
    data = b"".join(package.stream(entry))
    check_artifact_hash(entry, artifact, hashlib.sha256(data).hexdigest())

    def where(pointer: str) -> dict[str, Any]:
        return {
            "volume": entry.volume,
            "artifact": artifact.path,
            "offset": entry.offset,
            "pointer": pointer,
        }

    try:
        document = parse_json(data)
    except ValueError as error:
        raise _fail("artifact_invalid", str(error), **where("")) from None
    if not isinstance(document, dict) or document.keys() != {"keys"}:
        raise _fail("artifact_invalid", "se espera {keys}", **where(""))
    return _parse_key_items(document["keys"], where)


def verify_manifest_anchor(
    package: _Package, manifest: _Manifest, digest: str, report: ExportReport
) -> None:
    """La firma del SHA-256 del manifiesto con las claves incluidas (decisión A2).

    Sin ``manifest.signature.json`` el manifiesto queda sin firmar (``unanchored`` salvo
    ``--manifest-sha256``); una firma que no corresponde, de una clave desconocida, no vigente o
    que no verifica, rompe el paquete.
    """
    declared = manifest.document.get("manifest_signature")
    entry = package.entries.get(EXPORT_SIGNATURE_NAME)
    where: dict[str, Any] = {"volume": "vol-01.zip", "artifact": EXPORT_SIGNATURE_NAME}
    if entry is None:
        if declared is not None:
            raise _fail(
                "manifest_signature_invalid", "el manifiesto declara una firma que falta", **where
            )
        return
    where["offset"] = entry.offset
    if entry.volume != "vol-01.zip" or entry.info.file_size > MAX_SIGNATURE_BYTES:
        raise _fail("manifest_signature_invalid", "fuera de vol-01 o demasiado grande", **where)
    if declared != MANIFEST_SIGNATURE_DECLARATION:
        raise _fail(
            "manifest_signature_invalid",
            "el manifiesto no declara la firma que viaja",
            **{**where, "artifact": EXPORT_MANIFEST_NAME, "pointer": "/manifest_signature"},
        )
    keys = _included_keys(package, manifest)
    try:
        document = parse_json(b"".join(package.stream(entry)))
    except ValueError as error:
        raise _fail("manifest_signature_invalid", str(error), **where) from None
    if not isinstance(document, dict) or document.keys() != {"key_id", "payload", "signature"}:
        raise _fail("manifest_signature_invalid", "se espera {payload, key_id, signature}", **where)
    payload, key_id = document["payload"], document["key_id"]
    if (
        not isinstance(payload, dict)
        or payload.keys() != MANIFEST_SIGNATURE_PAYLOAD_KEYS
        or payload["kind"] != MANIFEST_SIGNATURE_KIND
        or not _matches(_STAMP_FORM, payload["generated_at"])
        or not isinstance(key_id, str)
    ):
        raise _fail("manifest_signature_invalid", "mensaje firmado no válido", **where)
    expected = {
        "manifest_sha256": digest,
        "export_id": manifest.document.get("export_id"),
        "organization_id": manifest.organization_id,
        "plant_id": manifest.plant_id,
        "generated_at": manifest.document.get("generated_at"),
    }
    for field_name, value in expected.items():
        if payload[field_name] != value:
            raise _fail("manifest_signature_mismatch", f"{field_name} no coincide", **where)
    key = keys.get(key_id)
    if key is None:
        raise _fail("unknown_key", f"key_id {key_id!r}", **where)
    generated_at = payload["generated_at"]
    if not key["valid_from"] <= generated_at < key["valid_until"]:
        raise _fail(
            "key_not_valid_at_signing",
            f"key_id {key_id!r} vigente de {key['valid_from']} a {key['valid_until']},"
            f" manifiesto de {generated_at}",
            **where,
        )
    signature = _canonical_base64(document["signature"], _SIGNATURE_FORM)
    if signature is None or len(signature) != 64:
        raise _fail("manifest_signature_invalid", "firma en base64 no canónico", **where)
    try:
        message = canonicalize(payload)
    except CanonicalizationError:
        raise _fail("manifest_signature_invalid", "sin forma canónica", **where) from None
    if not ed25519_verify(key["raw"], message, signature):
        raise _fail("bad_signature", f"key_id {key_id!r}", **where)
    report.anchored_by.append("manifest_signature")


# --- Contenido ----------------------------------------------------------------------------------


class _Content:
    """Las comprobaciones de contenido sobre los artefactos ya hasheados."""

    def __init__(
        self,
        package: _Package,
        manifest: _Manifest,
        documents: dict[str, Any],
        report: ExportReport,
    ) -> None:
        self.package = package
        self.manifest = manifest
        self.documents = documents
        self.report = report
        self.by_kind: dict[str, list[_Artifact]] = {}
        for artifact in manifest.artifacts:
            self.by_kind.setdefault(artifact.kind, []).append(artifact)

    def _where(self, artifact: _Artifact, pointer: str) -> dict[str, Any]:
        entry = self.package.entries[artifact.path]
        return {
            "volume": entry.volume,
            "artifact": artifact.path,
            "offset": entry.offset,
            "pointer": pointer,
        }

    def _mismatch(self, detail: str, pointer: str) -> ExportBroken:
        entry = self.package.entries[EXPORT_MANIFEST_NAME]
        return _fail(
            "manifest_mismatch",
            detail,
            volume=entry.volume,
            artifact=EXPORT_MANIFEST_NAME,
            offset=entry.offset,
            pointer=pointer,
        )

    def _single(self, kind: str) -> _Artifact:
        found = self.by_kind.get(kind, [])
        if len(found) != 1 or found[0].path not in self.documents:
            raise self._mismatch(f"se espera un artefacto «{kind}»", "/artifacts")
        return found[0]

    def _invalid(self, artifact: _Artifact, pointer: str, detail: str = "") -> ExportBroken:
        return _fail("artifact_invalid", detail, **self._where(artifact, pointer))

    def run(self) -> None:
        keys = self.public_keys()
        checkpoints = self.checkpoints(keys)
        self.audit(checkpoints)
        self.findings(checkpoints)
        self.declarations()

    # claves y puntos de control

    def public_keys(self) -> dict[str, dict[str, Any]]:
        artifact = self._single("public_key")
        document = self.documents[artifact.path]
        if not isinstance(document, dict) or document.keys() != {"keys"}:
            raise self._invalid(artifact, "", "se espera {keys}")
        items = document["keys"]
        if items != self.manifest.document["checkpoint_public_keys"]:
            raise self._mismatch(
                "las claves públicas difieren de su archivo", "/checkpoint_public_keys"
            )
        return _parse_key_items(items, lambda pointer: self._where(artifact, pointer))

    def checkpoints(self, keys: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
        artifact = self._single("checkpoint")
        document = self.documents[artifact.path]
        if not isinstance(document, dict) or document.keys() != {"checkpoints"}:
            raise self._invalid(artifact, "", "se espera {checkpoints}")
        items = document["checkpoints"]
        if items != self.manifest.document["checkpoints"]:
            raise self._mismatch("los puntos de control difieren de su archivo", "/checkpoints")
        if not isinstance(items, list):
            raise self._invalid(artifact, "/checkpoints")
        organization_id, plant_id = self.manifest.organization_id, self.manifest.plant_id
        chains: set[tuple[str, object]] = set()
        verified: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            pointer = f"/checkpoints/{index}"
            where = self._where(artifact, pointer)
            if not isinstance(item, dict) or item.keys() != _CHECKPOINT_ENTRY_KEYS:
                raise _fail("checkpoint_invalid", "claves inesperadas", **where)
            kind, chain_plant, content = item["kind"], item["plant_id"], item["content"]
            sequence = item["sequence"]
            if (
                kind not in ("ledger", "audit")
                or (kind == "audit" and chain_plant is not None)
                or (chain_plant is not None and chain_plant != plant_id)
                or (kind, chain_plant) in chains
                or not _matches(_UUID_FORM, item["entry_id"])
                or not _matches(_SHA256_FORM, item["entry_hash"])
                or not _is_count(sequence)
                or sequence < 2
                or not isinstance(content, dict)
                or content.keys() != CHECKPOINT_CONTENT_KEYS
            ):
                raise _fail("checkpoint_invalid", **where)
            chains.add((kind, chain_plant))
            covered_sequence, covered_hash = content["covered_sequence"], content["covered_hash"]
            taken_at, key_id = content["taken_at"], content["key_id"]
            if (
                not _is_count(covered_sequence)
                or covered_sequence != sequence - 1
                or not _matches(_SHA256_FORM, covered_hash)
                or not _matches(_STAMP_FORM, taken_at)
                or not isinstance(key_id, str)
            ):
                raise _fail("checkpoint_invalid", "cobertura o marca no válidas", **where)
            signature = _canonical_base64(content["signature"], _SIGNATURE_FORM)
            if signature is None or len(signature) != 64:
                raise _fail("checkpoint_invalid", "firma en base64 no canónico", **where)
            key = keys.get(key_id)
            if key is None:
                raise _fail("unknown_key", f"key_id {key_id!r}", **where)
            if not key["valid_from"] <= taken_at < key["valid_until"]:
                raise _fail(
                    "key_not_valid_at_signing",
                    f"key_id {key_id!r} vigente de {key['valid_from']} a {key['valid_until']},"
                    f" punto de control de {taken_at}",
                    **where,
                )
            try:
                message = checkpoint_message(
                    kind, organization_id, chain_plant, covered_sequence, covered_hash, taken_at
                )
            except CanonicalizationError:
                raise _fail("checkpoint_invalid", "sin forma canónica", **where) from None
            if not ed25519_verify(key["raw"], message, signature):
                raise _fail("bad_signature", f"key_id {key_id!r}", **where)
            verified.append({**item, "pointer": pointer})
            self.report.checkpoints_verified += 1
        return verified

    # cadenas

    def _anchor(
        self,
        links: dict[int, tuple[str, str, _Artifact, str]],
        checkpoints: list[dict[str, Any]],
        kind: str,
        chain_plant: str | None,
        hash_name: str,
    ) -> None:
        """Enlaces entre consecutivos y con lo que cubre el punto de control firmado."""
        for sequence in sorted(links):
            _, previous_hash, artifact, pointer = links[sequence]
            before = links.get(sequence - 1)
            if before is not None and previous_hash != before[0]:
                raise _fail(
                    "previous_hash_mismatch",
                    f"secuencia {sequence}",
                    **self._where(artifact, pointer + "/previous_hash"),
                )
        for checkpoint in checkpoints:
            if checkpoint["kind"] != kind or checkpoint["plant_id"] != chain_plant:
                continue
            content = checkpoint["content"]
            covered = links.get(content["covered_sequence"])
            if covered is not None and covered[0] != content["covered_hash"]:
                raise _fail(
                    "checkpoint_mismatch",
                    f"secuencia {content['covered_sequence']}",
                    **self._where(covered[2], covered[3] + "/" + hash_name),
                )
            own = links.get(checkpoint["sequence"])
            if own is not None and (
                own[0] != checkpoint["entry_hash"] or own[1] != content["covered_hash"]
            ):
                raise _fail(
                    "checkpoint_mismatch",
                    f"secuencia {checkpoint['sequence']}",
                    **self._where(own[2], own[3] + "/" + hash_name),
                )

    def audit(self, checkpoints: list[dict[str, Any]]) -> None:
        artifact = self._single("audit")
        document = self.documents[artifact.path]
        if not isinstance(document, dict) or document.keys() != {"entries"}:
            raise self._invalid(artifact, "", "se espera {entries}")
        entries = document["entries"]
        if not isinstance(entries, list):
            raise self._invalid(artifact, "/entries")
        links: dict[int, tuple[str, str, _Artifact, str]] = {}
        for index, entry in enumerate(entries):
            pointer = f"/entries/{index}"
            if not _audit_shape(entry):
                raise self._invalid(artifact, pointer, "entrada de auditoría")
            filters = entry["filters"]
            try:
                filters_hash = (
                    None if filters is None else hashlib.sha256(canonicalize(filters)).hexdigest()
                )
                envelope = canonicalize(_audit_envelope_export(entry, self.manifest, filters_hash))
            except CanonicalizationError:
                raise _fail("content_not_canonical", **self._where(artifact, pointer)) from None
            computed = hashlib.sha256(envelope + entry["previous_hash"].encode("ascii")).hexdigest()
            if computed != entry["entry_hash"]:
                raise _fail("entry_hash_mismatch", **self._where(artifact, pointer + "/entry_hash"))
            sequence = entry["chain_sequence"]
            known = links.get(sequence)
            if known is not None and known[0] != entry["entry_hash"]:
                raise _fail("record_conflict", **self._where(artifact, pointer))
            links[sequence] = (entry["entry_hash"], entry["previous_hash"], artifact, pointer)
            self.report.audit_entries_checked += 1
        self._anchor(links, checkpoints, "audit", None, "entry_hash")

    def findings(self, checkpoints: list[dict[str, Any]]) -> None:
        zones = self.manifest.document["zones"]
        zone_ids: list[object] = [zone.get("zone_id") for zone in zones if isinstance(zone, dict)]
        if len(zone_ids) != len(zones) or len(set(map(str, zone_ids))) != len(zone_ids):
            raise self._mismatch("zonas no válidas", "/zones")
        seen_zones: set[str] = set()
        links: dict[int, tuple[str, str, _Artifact, str]] = {}
        records: dict[str, tuple[str, str]] = {}
        states: dict[str, int] = {}
        flagged = 0
        for artifact in self.by_kind.get("findings_json", []):
            document = self.documents.get(artifact.path)
            if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
                raise self._invalid(artifact, "", "se espera {zone_id, findings}")
            zone_id = document.get("zone_id")
            if zone_id not in zone_ids or zone_id in seen_zones:
                raise self._mismatch(f"hallazgos de una zona no declarada: {zone_id!r}", "/zones")
            seen_zones.add(str(zone_id))
            for index, finding in enumerate(document["findings"]):
                pointer = f"/findings/{index}"
                if (
                    not isinstance(finding, dict)
                    or finding.get("zone_id") != zone_id
                    or not isinstance(finding.get("lifecycle_state"), str)
                    or not isinstance(finding.get("evidence_flagged"), bool)
                    or not isinstance(finding.get("records"), list)
                ):
                    raise self._invalid(artifact, pointer, "hallazgo")
                state = finding["lifecycle_state"]
                states[state] = states.get(state, 0) + 1
                flagged += finding["evidence_flagged"]
                for position, record in enumerate(finding["records"]):
                    self._record(artifact, f"{pointer}/records/{position}", record, links, records)
        if seen_zones != set(map(str, zone_ids)):
            raise self._mismatch("falta el archivo de hallazgos de alguna zona", "/zones")
        self._anchor(links, checkpoints, "ledger", self.manifest.plant_id, "record_hash")
        self._counts(states, flagged)

    def _record(
        self,
        artifact: _Artifact,
        pointer: str,
        record: object,
        links: dict[int, tuple[str, str, _Artifact, str]],
        records: dict[str, tuple[str, str]],
    ) -> None:
        if not (
            isinstance(record, dict)
            and _matches(_UUID_FORM, record.get("record_id"))
            and _is_count(record.get("chain_sequence"))
            and _matches(_SHA256_FORM, record.get("content_hash"))
            and _matches(_SHA256_FORM, record.get("previous_hash"))
            and _matches(_SHA256_FORM, record.get("record_hash"))
        ):
            raise self._invalid(artifact, pointer, "registro")
        if "content" in record:
            try:
                computed = hashlib.sha256(canonicalize(record["content"])).hexdigest()
            except CanonicalizationError:
                raise _fail(
                    "content_not_canonical", **self._where(artifact, pointer + "/content")
                ) from None
            if computed != record["content_hash"]:
                raise _fail(
                    "content_hash_mismatch", **self._where(artifact, pointer + "/content_hash")
                )
        record_id, sequence = record["record_id"], record["chain_sequence"]
        identity = (record["record_hash"], str(sequence))
        if records.setdefault(record_id, identity) != identity:
            raise _fail("record_conflict", **self._where(artifact, pointer))
        known = links.get(sequence)
        if known is not None and known[0] != record["record_hash"]:
            raise _fail("record_conflict", **self._where(artifact, pointer))
        links[sequence] = (record["record_hash"], record["previous_hash"], artifact, pointer)
        self.report.records_checked += 1

    def _counts(self, states: Mapping[str, int], flagged: int) -> None:
        document = self.manifest.document
        declared = document.get("state_counts")
        if not isinstance(declared, dict) or any(not _is_count(v) for v in declared.values()):
            raise self._mismatch("state_counts no es válido", "/state_counts")
        for state in set(declared) | set(states):
            if declared.get(state, 0) != states.get(state, 0):
                raise self._mismatch(f"el estado {state!r} no cuadra", f"/state_counts/{state}")
        total = sum(states.values())
        expected = {
            "finding_count": total,
            "open_finding_count": sum(states.get(state, 0) for state in OPEN_LIFECYCLE_STATES),
            "closed_finding_count": states.get("closed", 0),
            "unclosable_count": states.get("unclosable", 0),
            "discarded_count": states.get("discarded", 0),
            "suspended_count": states.get("suspended_by_reclassification", 0),
            "evidence_flagged_count": flagged,
        }
        for key, value in expected.items():
            if document.get(key) != value:
                raise self._mismatch(f"{key} no cuadra con los hallazgos", f"/{key}")

    # declaraciones del manifiesto frente a los artefactos

    def declarations(self) -> None:
        document = self.manifest.document
        artifacts = {artifact.path: artifact for artifact in self.manifest.artifacts}
        form = document.get("package_form")
        if form not in ("with_clips", "without_clips"):
            raise self._mismatch("package_form no válido", "/package_form")
        included: set[str] = set()
        for index, clip in enumerate(document["clips"]):
            pointer = f"/clips/{index}"
            if not isinstance(clip, dict) or clip.get("included") is not (
                clip.get("path") is not None
            ):
                raise self._mismatch("clip mal declarado", pointer)
            if clip["included"] != (form == "with_clips"):
                raise self._mismatch("el clip no corresponde a la forma del paquete", pointer)
            if clip["included"]:
                self._declared(artifacts, clip, "clip", pointer)
                included.add(clip["path"])
        if {a.path for a in self.by_kind.get("clip", [])} != included:
            raise self._mismatch("un clip del paquete no está declarado", "/clips")
        attached: set[str] = set()
        for index, attachment in enumerate(document["attachments"]):
            pointer = f"/attachments/{index}"
            if not isinstance(attachment, dict):
                raise self._mismatch("adjunto mal declarado", pointer)
            self._declared(artifacts, attachment, "attachment", pointer)
            attached.add(attachment["path"])
        if {a.path for a in self.by_kind.get("attachment", [])} != attached:
            raise self._mismatch("un adjunto del paquete no está declarado", "/attachments")
        absent: set[object] = set()
        for index, item in enumerate(document["absent_entries"]):
            if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                raise self._mismatch("entrada ausente mal declarada", f"/absent_entries/{index}")
            absent.add(item["kind"])
        for kind in ("document", "verifier"):
            present = len(self.by_kind.get(kind, []))
            if (kind in absent and present) or (kind not in absent and present != 1):
                raise self._mismatch(f"«{kind}» presente y ausente a la vez", "/absent_entries")
        verifiers = self.by_kind.get("verifier", [])
        absent_version = self.manifest.verifier_version == EXPORT_VERIFIER_ABSENT
        if absent_version != ("verifier" in absent) or (
            verifiers and verifiers[0].path != EXPORT_VERIFIER_NAME
        ):
            raise self._mismatch(
                "verifier_version no cuadra con el verificador", "/verifier_version"
            )

    def _declared(
        self, artifacts: Mapping[str, _Artifact], item: Mapping[str, Any], kind: str, pointer: str
    ) -> None:
        path = item.get("path")
        artifact = artifacts.get(path) if isinstance(path, str) else None
        if (
            artifact is None
            or artifact.kind != kind
            or artifact.sha256 != item.get("sha256")
            or artifact.size_bytes != item.get("size_bytes")
        ):
            raise self._mismatch(f"{kind} declarado sin su artefacto", pointer)


def _canonical_base64(text: object, pattern: re.Pattern[str]) -> bytes | None:
    if not isinstance(text, str) or pattern.fullmatch(text) is None:
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if base64.b64encode(raw).decode("ascii") == text else None


def _optional_uuid(value: object) -> bool:
    return value is None or _matches(_UUID_FORM, value)


def _audit_shape(entry: object) -> TypeGuard[dict[str, Any]]:
    if not isinstance(entry, dict) or entry.keys() != _AUDIT_EXPORT_KEYS:
        return False
    actor = entry["actor"]
    return (
        isinstance(actor, dict)
        and actor.keys() == _AUDIT_ACTOR_KEYS
        and _matches(_UUID_FORM, entry["entry_id"])
        and _is_count(entry["chain_sequence"])
        and _matches(_SHA256_FORM, entry["previous_hash"])
        and _matches(_SHA256_FORM, entry["entry_hash"])
        and _optional_uuid(entry["scope_plant_id"])
        and _optional_uuid(entry["scope_zone_id"])
        and _optional_uuid(entry["resource_id"])
        and _matches(_STAMP_FORM, entry["occurred_at"])
    )


def _audit_envelope_export(
    entry: Mapping[str, Any], manifest: _Manifest, filters_hash: str | None
) -> dict[str, Any]:
    """El sobre de U-02 de una entrada de auditoría (BR-NUC-60) a partir de su forma exportada."""
    resource_kind, resource_id = entry["resource_kind"], entry["resource_id"]
    return {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "correlation_id": entry["correlation_id"],
        "entry_id": entry["entry_id"],
        "filters_hash": filters_hash,
        "occurred_at": entry["occurred_at"],
        "operation": entry["operation"],
        "organization_id": manifest.organization_id,
        "outcome": entry["outcome"],
        "resource_ref": (
            None
            if resource_kind is None and resource_id is None
            else {"id": resource_id, "kind": resource_kind}
        ),
        "result_count": entry["result_count"],
        "scope": {"plant_id": entry["scope_plant_id"], "zone_id": entry["scope_zone_id"]},
    }


# --- Extracción ---------------------------------------------------------------------------------


def _extract(package: _Package, manifest: _Manifest, destination: Path) -> None:
    """Escribe el paquete ya verificado en ``destination`` (vacío o inexistente)."""
    created = not destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    expected = {artifact.path: artifact.sha256 for artifact in manifest.artifacts}
    names = [EXPORT_MANIFEST_NAME, *expected]
    if EXPORT_SIGNATURE_NAME in package.entries:
        names.insert(1, EXPORT_SIGNATURE_NAME)
    try:
        for name in names:
            entry = package.entries[name]
            where: dict[str, Any] = {
                "volume": entry.volume,
                "artifact": name,
                "offset": entry.offset,
            }
            target = root.joinpath(*name.split("/"))
            hasher = hashlib.sha256()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    for chunk in package.stream(entry):
                        hasher.update(chunk)
                        output.write(chunk)
            except OSError as error:
                # Nunca un traceback: una ruta que choca con otra o un disco lleno es un fallo
                # declarado, y lo escrito se borra.
                raise _fail("extract_failed", str(error), **where) from None
            if name in expected and hasher.hexdigest() != expected[name]:
                raise _fail("extract_failed", "el hash cambió al extraer", **where)
    except BaseException:
        if created:
            shutil.rmtree(root, ignore_errors=True)
        else:
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        raise


# --- Verificación -------------------------------------------------------------------------------


def verify_export(
    path: Path,
    *,
    manifest_sha256: str | None = None,
    extract_to: Path | None = None,
    limits: ExportLimits | None = None,
) -> ExportReport:
    """Verifica el paquete de ``path``; con ``extract_to``, lo extrae solo si está íntegro."""
    report = ExportReport(str(path))
    limits = limits if limits is not None else ExportLimits()
    package: _Package | None = None
    try:
        volumes = find_volumes(path)
        report.volumes = [volume.name for volume in volumes]
        package = _Package(volumes, limits)
        manifest, digest = _read_manifest(package, manifest_sha256)
        report.manifest_sha256 = digest
        report.declared_verifier_version = manifest.verifier_version
        if manifest_sha256 is not None:
            report.anchored_by.append("manifest_sha256")
        verify_manifest_anchor(package, manifest, digest, report)
        documents = verify_artifact_hashes(package, manifest, report)
        _Content(package, manifest, documents, report).run()
        # Sin ancla no hay «íntegro» y no se extrae nada (decisión B1).
        if extract_to is not None and report.anchored_by:
            _extract(package, manifest, extract_to)
            report.extracted_to = str(extract_to)
    except ExportBroken as broken:
        report.failure = broken.failure
    except RecursionError:
        report.failure = ExportFailure(
            "artifact_invalid", ExportPosition(), "anidamiento demasiado profundo"
        )
    finally:
        if package is not None:
            package.close()
    return report


def export_report_document(report: ExportReport) -> dict[str, Any]:
    """El resultado como documento JSON (el archivo de ``--out``)."""
    failure = report.failure
    broken: dict[str, Any] | None = None
    if failure is not None:
        broken = {
            "reason": failure.reason,
            "message": failure.message(),
            "volume": failure.position.volume,
            "artifact": failure.position.artifact,
            "offset": failure.position.offset,
            "pointer": failure.position.pointer,
        }
    return {
        "verifier": {"name": "vigia_verify", "version": EXPORT_VERIFIER_VERSION},
        "package": report.package,
        "status": report.status,
        "volumes": report.volumes,
        "manifest_sha256": report.manifest_sha256,
        "declared_verifier_version": report.declared_verifier_version,
        "artifacts_verified": report.artifacts_verified,
        "checkpoints_verified": report.checkpoints_verified,
        "records_checked": report.records_checked,
        "audit_entries_checked": report.audit_entries_checked,
        "anchored_by": report.anchored_by,
        "broken": broken,
        "extracted_to": report.extracted_to,
    }


def render_export_summary(report: ExportReport) -> str:
    """Resumen legible en español."""
    lines = [
        f"Verificador de paquetes exportados de Vigía ({EXPORT_VERIFIER_VERSION})",
        f"Paquete: {report.package}",
        f"Volúmenes: {', '.join(report.volumes) if report.volumes else 'ninguno'}",
    ]
    if report.manifest_sha256 is not None:
        lines.append(f"SHA-256 del manifiesto: {report.manifest_sha256}")
    if report.declared_verifier_version not in (
        None,
        EXPORT_VERIFIER_ABSENT,
        EXPORT_VERIFIER_VERSION,
    ):
        lines.append(
            f"Aviso: el paquete declara el verificador {report.declared_verifier_version}; "
            f"este es {EXPORT_VERIFIER_VERSION}."
        )
    lines.append(
        f"Artefactos con su hash: {report.artifacts_verified}; puntos de control firmados: "
        f"{report.checkpoints_verified}; registros: {report.records_checked}; entradas de "
        f"auditoría: {report.audit_entries_checked}"
    )
    failure = report.failure
    if failure is not None:
        position = failure.position
        where = ", ".join(
            part
            for part in (
                f"volumen {position.volume}" if position.volume else "",
                f"artefacto {position.artifact}" if position.artifact else "",
                f"desplazamiento {position.offset}" if position.offset is not None else "",
                f"puntero {position.pointer}" if position.pointer else "",
            )
            if part
        )
        lines.append(f"ROTO: {failure.message()}" + (f" [{where}]" if where else ""))
        lines.append("No se extrajo nada.")
    elif not report.anchored_by:
        lines.append(
            "SIN ANCLA: el paquete no trae una firma válida del manifiesto y no se dio "
            "--manifest-sha256. Las comprobaciones solo prueban que el paquete es coherente "
            "consigo mismo, no que sea el que produjo la plataforma: quien rehaga el manifiesto "
            "puede cambiar registros y estados sin que se note. No se extrajo nada."
        )
    else:
        anchors = {
            "manifest_signature": "firma del manifiesto con la clave de los puntos de control",
            "manifest_sha256": "SHA-256 del manifiesto registrado por la plataforma",
        }
        lines.append("Anclado por: " + "; ".join(anchors[item] for item in report.anchored_by))
        if report.extracted_to is not None:
            lines.append(f"Extraído en: {report.extracted_to}")
    results = {"intact": "ÍNTEGRO", "broken": "ROTO", "unanchored": "SIN ANCLA"}
    lines.append(f"Resultado: {results[report.status]}")
    return "\n".join(lines) + "\n"


def _export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vigia_verify.py",
        description=(
            "Verifica, sin red y sin acceso a la plataforma, un paquete exportado de Vigía: "
            "rechaza rutas y entradas peligrosas, comprueba el hash de cada artefacto antes de "
            "extraer nada, las firmas de los puntos de control y las cadenas."
        ),
        epilog=(
            "Código de salida: 0 si el paquete está íntegro y anclado; 1 si está roto o no se "
            "puede leer; 2 si la orden es incorrecta; 3 si es coherente pero sin ancla (ni firma "
            "del manifiesto ni --manifest-sha256)."
        ),
        formatter_class=_Formatter,
        add_help=False,
    )
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opciones"
    parser.add_argument(
        "package",
        metavar="paquete",
        type=Path,
        help="directorio con vol-01.zip, vol-02.zip… o la ruta de vol-01.zip",
    )
    parser.add_argument(
        "--manifest-sha256",
        dest="manifest_sha256",
        metavar="HEX",
        help="SHA-256 del manifiesto registrado por la plataforma (export_produced)",
    )
    parser.add_argument(
        "--extract",
        metavar="DIRECTORIO",
        type=Path,
        help="extrae el paquete en este directorio vacío, solo si está íntegro",
    )
    parser.add_argument(
        "--out", metavar="resultado.json", type=Path, help="archivo de resultado JSON"
    )
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y termina")
    parser.add_argument(
        "--version",
        action="version",
        version=f"vigia_verify {EXPORT_VERIFIER_VERSION}",
        help="muestra la versión y termina",
    )
    return parser


def _usage(message: str) -> int:
    sys.stderr.write(f"vigia_verify.py: error: {message}\n")
    return EXIT_USAGE


def export_main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: 0 íntegro y anclado, 1 roto, 2 orden incorrecta, 3 sin ancla."""
    arguments = _export_parser().parse_args(argv)
    package: Path = arguments.package
    if not package.exists() and not package.is_symlink():
        return _usage(f"no existe {str(package)!r}")
    expected = arguments.manifest_sha256
    if expected is not None and _SHA256_FORM.fullmatch(expected) is None:
        return _usage("--manifest-sha256 debe ser hexadecimal en minúsculas de 64 caracteres")
    destination: Path | None = arguments.extract
    if destination is not None and (
        destination.is_symlink()
        or (destination.exists() and (not destination.is_dir() or any(destination.iterdir())))
    ):
        return _usage("--extract debe ser un directorio vacío o inexistente")
    report = verify_export(package, manifest_sha256=expected, extract_to=destination)
    sys.stdout.write(render_export_summary(report))
    if arguments.out is not None:
        text = json.dumps(export_report_document(report), ensure_ascii=False, indent=2) + "\n"
        try:
            arguments.out.write_text(text, encoding="utf-8")
        except OSError as error:
            return _usage(f"no se pudo escribir {str(arguments.out)!r}: {error}")
    codes = {"intact": EXIT_INTACT, "broken": EXIT_BROKEN, "unanchored": EXIT_UNANCHORED}
    return codes[report.status]


if __name__ == "__main__":
    raise SystemExit(export_main())
