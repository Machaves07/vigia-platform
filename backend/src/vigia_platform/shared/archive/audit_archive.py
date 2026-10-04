"""Tarea ``archive_audit_partitions``: archivado mensual de auditoría (LC-NUC-33, MAN-03).

La auditoría vive 24 meses en línea (NFR-NUC-32). La tarea, **mensual** (día 2 a las 03:00 UTC) y
con arrendamiento, toma cada partición mensual adjunta de ``shared.audit_entry`` cuyo mes terminó
hace al menos ``AUDIT_ONLINE_MONTHS`` meses según el reloj inyectado y, por cada una:

1. **Exporta** la partición entera (todas las organizaciones, por lotes y en orden de organización
   y secuencia; ``shared.vigia_audit_partition_rows`` de nuc_0016) y recorre cada cadena con
   ``chain_walk`` antes de subir nada: una partición que ya está rota en la base no se archiva.
2. **Empaqueta** un ZIP comprimido (``ARCHIVE_CONTENT_TYPE``) con:

   - ``archive.json``: formato, partición, mes, recuento, el SHA-256 del verificador y, por
     organización, recuento, secuencias, enlace con la partición anterior, cabeza y el **último
     punto de control** de la cadena dentro de la partición (``null`` si no tiene ninguno: la
     cadena de esa organización solo se ancla por el enlace);
   - ``organizations/<organization_id>/``: un paquete ``vigia-package`` (``docs/package-format.md``)
     con la cadena de auditoría de esa organización en JSON por líneas, desde la primera
     secuencia de la partición, y las claves públicas ``checkpoint`` (también las retiradas);
   - ``vigia_verify.py``: la copia del verificador, que comprueba cada paquete sin red ni secretos.

3. **Sube** el archivo a ``vigia-archive`` (cifrado ``aws:kms`` con la clave ``vigia-archive``,
   suma SHA-256 calculada por el almacén), en ``audit/<AAAA-MM>/audit_entry_AAAA_MM.zip``.
4. **Lo lee de vuelta** del almacén y lo **verifica**: mismo SHA-256 que lo subido; recuento por
   organización y total igual al de la base; cada cadena íntegra con ``chain_walk`` (enlaces, hashes
   y firma de cada punto de control con las claves publicadas); cada entrada, devuelta a columnas,
   **igual** a la fila de la base (hashes incluidos); puntos de control iguales a los declarados; y
   el verificador incluido, el esperado.
5. Solo entonces, en **una** transacción: ``shared.vigia_detach_audit_partition`` (comprueba de
   nuevo el recuento bajo bloqueo exclusivo, desprende la partición y la deja de solo anexar) y el
   registro ``audit_partition_archived`` en la cadena de expediente de la **organización
   proveedora** (``EscritorExpediente`` con la transacción del llamador): o quedan los dos o
   ninguno.

Si la verificación falla, la partición **sigue adjunta**, no se escribe ``audit_partition_archived``
y se alerta: ``security_alert`` (``alert_kind = audit_archive_verification_failed``) y una entrada
de auditoría ``integrity_verification`` con resultado ``error`` en la cadena de la proveedora, en
su propia transacción; después la tarea termina en ``AuditArchiveFailed`` para que el planificador
la cuente como fallida. Un fallo transitorio (almacén o base caídos) no alerta: la tarea falla y la
siguiente pasada vuelve a empezar. Repetir es seguro: el objeto se sobrescribe (otra versión, con
bloqueo de objetos) y una partición ya desprendida no vuelve a aparecer.

``restore_audit_partition`` es la restauración **de solo lectura** para investigación
(``vigia-admin restore-audit-partition``, TASK-132; formato y orden en
``docs/audit-archive-format.md``, runbook 6.5 en TASK-152): descarga, comprueba el SHA-256
registrado, verifica las cadenas con las claves del propio archivo y devuelve las filas con las
columnas de ``shared.audit_entry``; ``extract_archive`` escribe los paquetes y el verificador en un
directorio nuevo. Nunca escribe en la base: volver a cargar las filas es un paso del runbook.

**Lista ampliada** (TASK-203, PAT-GOB-ESC-02): en la misma pasada, después de la auditoría, la
tarea archiva las particiones vencidas de ``fleet.heartbeat_history`` (90 días),
``fleet.enrollment_attempt`` y ``fleet.fleet_alarm`` (24 meses) con ``table_archive.TableArchiver``:
mismo almacén, misma verificación de vuelta antes de desprender, mismo registro
``audit_partition_archived`` (versión 2) y misma alerta si algo falla. No hay octava tarea.

El planificador invoca el manejador una vez por organización; el archivado es global y solo actúa
en la iteración de la organización proveedora. No lee la hora del sistema.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.ledger.chain.chain_walk import (
    ChainRef,
    ChainWalker,
    CheckpointSeen,
    genesis_hash,
)
from vigia_platform.ledger.chain.checkpoints import CheckpointPublicKey
from vigia_platform.ledger.chain.package_verifier import parse_json
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError, canonicalize
from vigia_platform.ledger.chain.verify import audit_entry
from vigia_platform.shared.archive.errors import ArchiveFailure, ArchiveVerificationFailed, sqlstate
from vigia_platform.shared.archive.partitions import add_months, month_of
from vigia_platform.shared.archive.table_archive import (
    ARCHIVED_TABLES,
    ArchivedTable,
    ArchivedTablePartition,
    PartitionStillOpen,
    TableArchiver,
    TablePartition,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction, TransientDatabaseError
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StoragePort, StorageUnavailable

__all__ = [
    "ARCHIVED_RECORD_TYPE",
    "ARCHIVE_AUDIT_PARTITIONS",
    "ARCHIVE_AUDIT_PARTITIONS_SCHEDULE",
    "ARCHIVE_CONTENT_TYPE",
    "ARCHIVE_FORMAT",
    "ARCHIVE_FORMAT_VERSION",
    "AUDIT_ONLINE_MONTHS",
    "SECURITY_ALERT_KIND",
    "ArchiveContents",
    "ArchiveFailure",
    "ArchiveVerificationFailed",
    "ArchivedPartition",
    "AuditArchiveFailed",
    "AuditArchiver",
    "AuditPartition",
    "OrganizationSegment",
    "PartitionSnapshot",
    "RestoredPartition",
    "archive_audit_partitions_handler",
    "build_archive",
    "entry_line",
    "entry_row",
    "extract_archive",
    "read_archive",
    "register_archive_audit_partitions",
    "restore_audit_partition",
    "restored_from_bytes",
    "verify_archive",
]

_log = get_logger("shared.archive")

ARCHIVE_AUDIT_PARTITIONS: Final = "archive_audit_partitions"
ARCHIVE_AUDIT_PARTITIONS_SCHEDULE: Final = Schedule.monthly(day=2, hour=3)
"""Mensual, el día 2 a las 03:00 UTC ``[objetivo propio]``: después del punto de control diario
del día 1, que cubre el final del mes anterior."""
AUDIT_ONLINE_MONTHS: Final = 24
"""Meses de auditoría en línea (NFR-NUC-32): se archiva la partición cuyo mes terminó hace 24."""

ARCHIVED_RECORD_TYPE: Final = "audit_partition_archived"
SECURITY_ALERT_EVENT: Final = "security_alert"
SECURITY_ALERT_KIND: Final = "audit_archive_verification_failed"
ARCHIVE_CHECK: Final = "audit_archive"

ARCHIVE_FORMAT: Final = "vigia-audit-archive"
ARCHIVE_FORMAT_VERSION: Final = 1
ARCHIVE_CONTENT_TYPE: Final = "application/zip"
ARCHIVE_MANIFEST: Final = "archive.json"
VERIFIER_FILE: Final = "vigia_verify.py"
PACKAGE_FORMAT: Final = "vigia-package"
PACKAGE_FORMAT_VERSION: Final = 1
CHAIN_FILE: Final = "chains/audit.jsonl"
MAX_MEMBER_BYTES: Final = 1024 * 1024 * 1024
"""Tope de un miembro del ZIP al leerlo (1 GiB): un archivo manipulado no agota la memoria."""
READ_BATCH: Final = 5_000
"""Filas por lote al exportar ``[objetivo propio]``: cada sentencia muy por debajo de 30 s."""

_PARTITION_NAME: Final = re.compile(r"audit_entry_([0-9]{4})_(0[1-9]|1[0-2])")
_UUID_TEXT: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)

_ATTACHED_PARTITIONS: Final = text(
    "SELECT child.relname AS name FROM pg_catalog.pg_inherits AS inheritance"
    " JOIN pg_catalog.pg_class AS child ON child.oid = inheritance.inhrelid"
    " WHERE inheritance.inhparent = 'shared.audit_entry'::regclass ORDER BY child.relname"
)
_SUMMARY: Final = text(
    "SELECT organization_id, entries, first_sequence, last_sequence"
    " FROM shared.vigia_audit_partition_summary(:partition)"
)
_ROWS: Final = text(
    "SELECT * FROM shared.vigia_audit_partition_rows(:partition, :after_organization,"
    " :after_sequence, :max_rows)"
)
_DETACH: Final = text(
    "SELECT shared.vigia_detach_audit_partition(:partition, :expected_entries, :not_after)"
)
_DETACH_REJECTED: Final = frozenset({"55000", "42P01"})
"""Rechazos de ``vigia_detach_audit_partition``: ``object_not_in_prerequisite_state`` (la
partición no es la verificada) y ``undefined_table`` (ya no está adjunta)."""


_sqlstate = sqlstate


AUDIT_COLUMNS: Final = (
    "entry_id",
    "organization_id",
    "chain_sequence",
    "actor_kind",
    "actor_id",
    "actor_display_name_snapshot",
    "actor_role_in_use",
    "actor_concession_id",
    "actor_unit",
    "operation",
    "scope_plant_id",
    "scope_zone_id",
    "resource_kind",
    "resource_id",
    "filters",
    "filters_hash",
    "result_count",
    "outcome",
    "correlation_id",
    "occurred_at",
    "previous_hash",
    "entry_hash",
)
"""Las columnas de ``shared.audit_entry`` que el archivo conserva; ``filters_json`` es generada."""
_UUID_COLUMNS: Final = frozenset(
    {
        "entry_id",
        "organization_id",
        "actor_id",
        "actor_concession_id",
        "scope_plant_id",
        "scope_zone_id",
        "resource_id",
        "correlation_id",
    }
)


# --- Errores ------------------------------------------------------------------------------------
# ``ArchiveFailure`` y ``ArchiveVerificationFailed`` viven en ``errors``: los comparte el archivado
# de las tablas de ``fleet`` (``table_archive``).


class AuditArchiveFailed(Exception):
    """Alguna partición vencida quedó sin archivar en esta pasada (el planificador la cuenta)."""

    code: Final = "audit_archive_failed"

    def __init__(self, partitions: Sequence[str]) -> None:
        super().__init__(f"particiones sin archivar: {', '.join(partitions)}")
        self.partitions = tuple(partitions)


# --- Valores ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class AuditPartition:
    """Una partición mensual de ``shared.audit_entry`` (``audit_entry_AAAA_MM``)."""

    month: date

    def __post_init__(self) -> None:
        if not isinstance(self.month, date) or isinstance(self.month, datetime):
            raise TypeError("month debe ser una fecha")
        if self.month.day != 1:
            raise ValueError("month debe ser el primer día del mes")

    @classmethod
    def from_name(cls, name: str) -> AuditPartition | None:
        match = _PARTITION_NAME.fullmatch(name) if isinstance(name, str) else None
        if match is None:
            return None
        return cls(date(int(match.group(1)), int(match.group(2)), 1))

    @property
    def name(self) -> str:
        return f"audit_entry_{self.month.year:04d}_{self.month.month:02d}"

    @property
    def qualified_name(self) -> str:
        return f"shared.{self.name}"

    @property
    def period(self) -> str:
        return f"{self.month.year:04d}-{self.month.month:02d}"

    @property
    def range_end(self) -> datetime:
        end = add_months(self.month, 1)
        return datetime(end.year, end.month, 1, tzinfo=UTC)

    @property
    def object_key(self) -> str:
        return f"audit/{self.period}/{self.name}.zip"


@dataclass(frozen=True, slots=True)
class OrganizationSegment:
    """Lo que el archivo declara de la cadena de una organización dentro de la partición."""

    organization_id: uuid.UUID
    entries: int
    first_sequence: int
    last_sequence: int
    first_previous_hash: str
    last_hash: str
    checkpoints: int
    last_checkpoint: dict[str, Any] | None

    @property
    def directory(self) -> str:
        return f"organizations/{self.organization_id}"

    def to_json(self) -> dict[str, Any]:
        return {
            "organization_id": str(self.organization_id),
            "package": self.directory,
            "entries": self.entries,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "first_previous_hash": self.first_previous_hash,
            "last_hash": self.last_hash,
            "checkpoints": self.checkpoints,
            "last_checkpoint": self.last_checkpoint,
        }


@dataclass(frozen=True, slots=True)
class PartitionSnapshot:
    """La partición leída de la base: filas por organización, en orden de secuencia."""

    partition: AuditPartition
    rows: Mapping[uuid.UUID, Sequence[Mapping[str, Any]]]

    @property
    def entry_count(self) -> int:
        return sum(len(rows) for rows in self.rows.values())


@dataclass(frozen=True, slots=True)
class ArchiveContents:
    """Un archivo leído: su manifiesto, las entradas de cada organización y el verificador."""

    manifest: dict[str, Any]
    segments: tuple[OrganizationSegment, ...]
    entries: dict[uuid.UUID, list[dict[str, Any]]]
    package_keys: dict[uuid.UUID, dict[str, bytes]]
    verifier: bytes

    @property
    def partition(self) -> AuditPartition | None:
        name = self.manifest.get("partition")
        return AuditPartition.from_name(name) if isinstance(name, str) else None


@dataclass(frozen=True, slots=True)
class ArchivedPartition:
    """Una partición archivada, verificada y desprendida en esta pasada."""

    partition: AuditPartition
    object_key: str
    sha256: str
    entry_count: int
    archived_at: datetime


@dataclass(frozen=True, slots=True)
class RestoredPartition:
    """La restauración de solo lectura: el archivo y sus filas como columnas de la base."""

    contents: ArchiveContents
    rows: tuple[dict[str, Any], ...]


@dataclass(slots=True)
class ArchiveRunReport:
    """Lo que hizo una pasada de la tarea: la auditoría y las tablas de la lista ampliada."""

    archived: list[ArchivedPartition | ArchivedTablePartition] = field(default_factory=list)
    failed: list[tuple[AuditPartition | TablePartition, str]] = field(default_factory=list)
    deferred: list[TablePartition] = field(default_factory=list)
    """Particiones vencidas con filas abiertas (alarmas sin cerrar): siguen adjuntas, sin alerta."""


# --- Puertos ------------------------------------------------------------------------------------


class ArchiveDatabase(Protocol):
    """``shared.db.Database``."""

    def transaction(self, context: ScopeContext) -> Any: ...


class ArchiveRecordWriter(Protocol):
    """``EscritorExpediente.write`` con la transacción del llamador."""

    async def write(
        self,
        context: ScopeContext | None,
        record_type: str,
        content: Mapping[str, Any],
        *,
        transaction: Transaction | None = None,
    ) -> object: ...


class ArchiveAudit(Protocol):
    """``AuditWriter.append``."""

    async def append(
        self,
        context: ScopeContext,
        operation: str,
        *,
        outcome: str = ...,
        filters: Mapping[str, Any] | None = None,
        result_count: int | None = None,
        transaction: Transaction | None = None,
    ) -> object: ...


# --- Formato: filas, líneas y archivo -----------------------------------------------------------


def entry_line(row: Mapping[str, Any]) -> bytes:
    """La línea JSON de una fila, en la forma del paquete (``docs/package-format.md`` §3.2).

    ``filters`` va con los **bytes de la columna** tal cual (ya son el canónico RFC 8785), como
    recomienda el formato: así la línea conserva exactamente lo que cubre ``filters_hash``.
    """
    entry = audit_entry(row)
    entry.pop("filters")
    rest = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    filters = row["filters"]
    raw = b"null" if filters is None else bytes(filters)
    return b'{"filters":' + raw + b"," + rest[1:]


def _uuid_value(value: object, column: str) -> uuid.UUID | None:
    if value is None:
        return None
    if not isinstance(value, str) or _UUID_TEXT.fullmatch(value) is None:
        raise ValueError(f"{column} no es un UUID en minúsculas")
    return uuid.UUID(value)


def _timestamp_value(value: object) -> datetime:
    """``AAAA-MM-DDTHH:MM:SS.mmmZ`` → marca UTC; el texto debe ser exactamente el de la base."""
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("occurred_at no es una marca UTC con milisegundos")
    moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    if format_timestamp(moment) != value:
        raise ValueError("occurred_at no es una marca UTC con milisegundos")
    return moment


def entry_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Una entrada del paquete devuelta a las columnas de ``shared.audit_entry``.

    Inversa de ``entry_line`` sobre una entrada ya recorrida por ``chain_walk``: ``filters`` vuelve
    a ser el canónico RFC 8785 del documento, que es la columna si ``filters_hash`` coincide.
    ``ValueError`` si la entrada no tiene la forma.
    """
    actor = entry["actor"]
    scope = entry["scope"]
    resource = entry["resource_ref"]
    filters = entry["filters"]
    try:
        filters_bytes = None if filters is None else canonicalize(filters)
    except CanonicalizationError as error:
        raise ValueError("filters no es canonicalizable") from error
    row = {
        "entry_id": _uuid_value(entry["entry_id"], "entry_id"),
        "organization_id": _uuid_value(entry["organization_id"], "organization_id"),
        "chain_sequence": entry["chain_sequence"],
        "actor_kind": actor["kind"],
        "actor_id": _uuid_value(actor["id"], "actor_id"),
        "actor_display_name_snapshot": actor["display_name_snapshot"],
        "actor_role_in_use": actor["role_in_use"],
        "actor_concession_id": _uuid_value(actor["concession_id"], "actor_concession_id"),
        "actor_unit": actor["unit"],
        "operation": entry["operation"],
        "scope_plant_id": _uuid_value(scope["plant_id"], "scope_plant_id"),
        "scope_zone_id": _uuid_value(scope["zone_id"], "scope_zone_id"),
        "resource_kind": None if resource is None else resource["kind"],
        "resource_id": None if resource is None else _uuid_value(resource["id"], "resource_id"),
        "filters": filters_bytes,
        "filters_hash": entry["filters_hash"],
        "result_count": entry["result_count"],
        "outcome": entry["outcome"],
        "correlation_id": _uuid_value(entry["correlation_id"], "correlation_id"),
        "occurred_at": _timestamp_value(entry["occurred_at"]),
        "previous_hash": entry["previous_hash"],
        "entry_hash": entry["entry_hash"],
    }
    return row


def column_values(row: Mapping[str, Any]) -> dict[str, Any]:
    """Las columnas archivadas de una fila de la base, normalizadas para comparar."""
    values: dict[str, Any] = {}
    for column in AUDIT_COLUMNS:
        value = row[column]
        if column in _UUID_COLUMNS and value is not None:
            value = uuid.UUID(str(value))
        elif column == "filters" and value is not None:
            value = bytes(value)
        elif column == "occurred_at":
            value = value.astimezone(UTC)
        values[column] = value
    return values


def _checkpoint_json(seen: CheckpointSeen) -> dict[str, Any]:
    return {
        "sequence": seen.sequence,
        "entry_id": seen.entry_id,
        "entry_hash": seen.entry_hash,
        "covered_sequence": seen.covered_sequence,
        "covered_hash": seen.covered_hash,
        "key_id": seen.key_id,
        "taken_at": seen.taken_at,
    }


def _walk(
    organization_id: uuid.UUID,
    entries: Iterable[object],
    first_sequence: int,
    first_previous_hash: str,
    keys: Mapping[str, bytes],
    declared_head: tuple[int, str] | None = None,
) -> tuple[bool, tuple[CheckpointSeen, ...], str]:
    walker = ChainWalker(
        ChainRef("audit", str(organization_id), None),
        keys,
        start_sequence=first_sequence - 1,
        start_hash=first_previous_hash,
    )
    for entry in entries:
        if walker.feed(entry) is not None:
            break
    result = walker.finish(declared_head)
    detail = "" if result.broken is None else result.broken.reason
    return result.intact, result.checkpoints, detail


def _key_map(keys: Sequence[CheckpointPublicKey]) -> dict[str, bytes]:
    return {key.key_id: key.public_key_bytes() for key in keys}


def _segment(
    organization_id: uuid.UUID, rows: Sequence[Mapping[str, Any]], keys: Mapping[str, bytes]
) -> OrganizationSegment:
    """Recorre la cadena de la base antes de exportarla y declara su segmento."""
    if not rows:
        raise ArchiveVerificationFailed(ArchiveFailure.SOURCE_BROKEN, "organización sin filas")
    first, last = rows[0], rows[-1]
    first_sequence = int(first["chain_sequence"])
    first_previous = str(first["previous_hash"])
    if first_sequence == 1 and first_previous != genesis_hash(str(organization_id), None):
        raise ArchiveVerificationFailed(ArchiveFailure.SOURCE_BROKEN, "génesis")
    intact, checkpoints, detail = _walk(
        organization_id,
        (audit_entry(row) for row in rows),
        first_sequence,
        first_previous,
        keys,
    )
    if not intact:
        raise ArchiveVerificationFailed(ArchiveFailure.SOURCE_BROKEN, detail)
    return OrganizationSegment(
        organization_id=organization_id,
        entries=len(rows),
        first_sequence=first_sequence,
        last_sequence=int(last["chain_sequence"]),
        first_previous_hash=first_previous,
        last_hash=str(last["entry_hash"]),
        checkpoints=len(checkpoints),
        last_checkpoint=_checkpoint_json(checkpoints[-1]) if checkpoints else None,
    )


def _zip_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def _json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )


def build_archive(
    snapshot: PartitionSnapshot,
    keys: Sequence[CheckpointPublicKey],
    verifier: bytes,
    exported_at: datetime,
) -> bytes:
    """El ZIP de una partición; recorre cada cadena antes (``SOURCE_BROKEN`` si ya está rota)."""
    key_map = _key_map(keys)
    segments = [
        _segment(organization_id, snapshot.rows[organization_id], key_map)
        for organization_id in sorted(snapshot.rows)
    ]
    partition = snapshot.partition
    manifest = {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_FORMAT_VERSION,
        "partition": partition.name,
        "period": partition.period,
        "exported_at": format_timestamp(exported_at),
        "entry_count": snapshot.entry_count,
        "verifier": VERIFIER_FILE,
        "verifier_sha256": hashlib.sha256(verifier).hexdigest(),
        "organizations": [segment.to_json() for segment in segments],
    }
    # Exactamente las dos claves que lee el verificador (``docs/package-format.md`` §2).
    key_documents = [{"key_id": key.key_id, "public_key": key.public_key} for key in keys]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        _zip_member(archive, ARCHIVE_MANIFEST, _json_bytes(manifest))
        _zip_member(archive, VERIFIER_FILE, verifier)
        for segment in segments:
            rows = snapshot.rows[segment.organization_id]
            package = {
                "format": PACKAGE_FORMAT,
                "format_version": PACKAGE_FORMAT_VERSION,
                "organization_id": str(segment.organization_id),
                "chains": [
                    {
                        "kind": "audit",
                        "plant_id": None,
                        "file": CHAIN_FILE,
                        "first_sequence": segment.first_sequence,
                        "last_sequence": segment.last_sequence,
                        "last_hash": segment.last_hash,
                    }
                ],
                "checkpoint_keys": key_documents,
                "partition": partition.name,
                "period": partition.period,
            }
            lines = b"".join(entry_line(row) + b"\n" for row in rows)
            _zip_member(archive, f"{segment.directory}/manifest.json", _json_bytes(package))
            _zip_member(archive, f"{segment.directory}/{CHAIN_FILE}", lines)
    return buffer.getvalue()


# --- Lectura ------------------------------------------------------------------------------------


def _unreadable(detail: str) -> ArchiveVerificationFailed:
    return ArchiveVerificationFailed(ArchiveFailure.UNREADABLE, detail)


def _json_member(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    document = parse_json(_member(archive, name))
    if not isinstance(document, dict):
        raise _unreadable(f"{name} no es un objeto JSON")
    return document


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise _unreadable(f"{name} supera el tope")
    return archive.read(info)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unreadable("se esperaba un entero")
    return value


def _hex64(value: object) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise _unreadable("se esperaba un hash")
    return value


def _read_segment(item: object) -> OrganizationSegment:
    expected = {
        "organization_id",
        "package",
        "entries",
        "first_sequence",
        "last_sequence",
        "first_previous_hash",
        "last_hash",
        "checkpoints",
        "last_checkpoint",
    }
    if not isinstance(item, dict) or set(item) != expected:
        raise _unreadable("organización mal formada en archive.json")
    organization_id = _uuid_value(item["organization_id"], "organization_id")
    if organization_id is None:
        raise _unreadable("organización sin identificador")
    checkpoint = item["last_checkpoint"]
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise _unreadable("last_checkpoint mal formado")
    segment = OrganizationSegment(
        organization_id=organization_id,
        entries=_int(item["entries"]),
        first_sequence=_int(item["first_sequence"]),
        last_sequence=_int(item["last_sequence"]),
        first_previous_hash=_hex64(item["first_previous_hash"]),
        last_hash=_hex64(item["last_hash"]),
        checkpoints=_int(item["checkpoints"]),
        last_checkpoint=checkpoint,
    )
    if item["package"] != segment.directory:
        raise _unreadable("ruta de paquete inesperada")
    return segment


def _read_lines(data: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if data and not data.endswith(b"\n"):
        raise _unreadable("la cadena no termina en salto de línea")
    for line in data.split(b"\n")[:-1] if data else []:
        entry = parse_json(line)
        if not isinstance(entry, dict):
            raise _unreadable("línea que no es un objeto JSON")
        entries.append(entry)
    return entries


def _public_key(value: object) -> bytes:
    """Ed25519 de 32 bytes en base64 estándar canónico, como en ``checkpoint_keys``."""
    if not isinstance(value, str):
        raise _unreadable("clave pública mal formada")
    try:
        public = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _unreadable("clave pública mal formada") from None
    if len(public) != 32 or base64.b64encode(public).decode("ascii") != value:
        raise _unreadable("clave pública mal formada")
    return public


def _package_keys(package: Mapping[str, Any], segment: OrganizationSegment) -> dict[str, bytes]:
    chains = package.get("chains")
    expected_chain = {
        "kind": "audit",
        "plant_id": None,
        "file": CHAIN_FILE,
        "first_sequence": segment.first_sequence,
        "last_sequence": segment.last_sequence,
        "last_hash": segment.last_hash,
    }
    if (
        package.get("format") != PACKAGE_FORMAT
        or package.get("format_version") != PACKAGE_FORMAT_VERSION
        or package.get("organization_id") != str(segment.organization_id)
        or chains != [expected_chain]
    ):
        raise ArchiveVerificationFailed(ArchiveFailure.FORMAT_MISMATCH, "paquete")
    keys: dict[str, bytes] = {}
    items = package.get("checkpoint_keys")
    if not isinstance(items, list):
        raise _unreadable("checkpoint_keys")
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"key_id", "public_key"}
            or not isinstance(item["key_id"], str)
            or item["key_id"] in keys
        ):
            raise _unreadable("checkpoint_keys")
        keys[item["key_id"]] = _public_key(item["public_key"])
    return keys


def read_archive(data: bytes) -> ArchiveContents:
    """Lee un archivo sin extraer nada: miembros exactos, JSON estricto y topes de tamaño."""
    if not isinstance(data, bytes | bytearray):
        raise TypeError("data debe ser bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            manifest = _json_member(archive, ARCHIVE_MANIFEST)
            if (
                manifest.get("format") != ARCHIVE_FORMAT
                or manifest.get("format_version") != ARCHIVE_FORMAT_VERSION
            ):
                raise ArchiveVerificationFailed(ArchiveFailure.FORMAT_MISMATCH, "archive.json")
            organizations = manifest.get("organizations")
            if not isinstance(organizations, list):
                raise _unreadable("organizations")
            segments = tuple(_read_segment(item) for item in organizations)
            identifiers = [segment.organization_id for segment in segments]
            if identifiers != sorted(set(identifiers)):
                raise _unreadable("organizaciones repetidas o fuera de orden")
            expected_names = {ARCHIVE_MANIFEST, VERIFIER_FILE}
            for segment in segments:
                expected_names.add(f"{segment.directory}/manifest.json")
                expected_names.add(f"{segment.directory}/{CHAIN_FILE}")
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != expected_names:
                raise _unreadable("miembros del archivo distintos de los declarados")
            entries: dict[uuid.UUID, list[dict[str, Any]]] = {}
            package_keys: dict[uuid.UUID, dict[str, bytes]] = {}
            for segment in segments:
                package = _json_member(archive, f"{segment.directory}/manifest.json")
                package_keys[segment.organization_id] = _package_keys(package, segment)
                entries[segment.organization_id] = _read_lines(
                    _member(archive, f"{segment.directory}/{CHAIN_FILE}")
                )
            verifier = _member(archive, VERIFIER_FILE)
    except ArchiveVerificationFailed:
        raise
    except Exception as error:
        # ZIP corrupto (BadZipFile, CRC), miembro ausente (KeyError), JSON no válido
        # (ValueError) o fallo de descompresión (zlib.error): nada de eso se da por bueno.
        raise _unreadable(type(error).__name__) from None
    return ArchiveContents(manifest, segments, entries, package_keys, verifier)


# --- Verificación -------------------------------------------------------------------------------


def _self_consistent(
    contents: ArchiveContents, keys_for: Callable[[uuid.UUID], Mapping[str, bytes]]
) -> None:
    """Recuentos, cadenas y puntos de control del archivo contra lo que él mismo declara."""
    total = 0
    for segment in contents.segments:
        entries = contents.entries[segment.organization_id]
        if len(entries) != segment.entries or segment.entries < 1:
            raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "recuento declarado")
        total += len(entries)
        if segment.first_sequence == 1 and segment.first_previous_hash != genesis_hash(
            str(segment.organization_id), None
        ):
            raise ArchiveVerificationFailed(ArchiveFailure.CHAIN_BROKEN, "génesis")
        intact, checkpoints, detail = _walk(
            segment.organization_id,
            entries,
            segment.first_sequence,
            segment.first_previous_hash,
            keys_for(segment.organization_id),
            (segment.last_sequence, segment.last_hash),
        )
        if not intact:
            raise ArchiveVerificationFailed(ArchiveFailure.CHAIN_BROKEN, detail)
        declared = segment.last_checkpoint
        found = _checkpoint_json(checkpoints[-1]) if checkpoints else None
        if len(checkpoints) != segment.checkpoints or declared != found:
            raise ArchiveVerificationFailed(ArchiveFailure.CHECKPOINT_MISMATCH)
    if contents.manifest.get("entry_count") != total:
        raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "recuento total")
    if contents.manifest.get("verifier_sha256") != hashlib.sha256(contents.verifier).hexdigest():
        raise ArchiveVerificationFailed(ArchiveFailure.VERIFIER_MISMATCH)


def restore_rows(contents: ArchiveContents) -> tuple[dict[str, Any], ...]:
    """Las filas del archivo como columnas de ``shared.audit_entry``, en orden de organización y
    secuencia (``restore`` de PR-NUC-56)."""
    rows: list[dict[str, Any]] = []
    try:
        for segment in contents.segments:
            rows.extend(entry_row(entry) for entry in contents.entries[segment.organization_id])
    except (KeyError, TypeError, ValueError) as error:
        raise ArchiveVerificationFailed(ArchiveFailure.UNREADABLE, type(error).__name__) from None
    return tuple(rows)


def verify_archive(
    contents: ArchiveContents,
    snapshot: PartitionSnapshot,
    keys: Sequence[CheckpointPublicKey],
    verifier: bytes,
) -> None:
    """Verificación de vuelta contra la base; ``ArchiveVerificationFailed`` al primer fallo.

    Formato y partición; el verificador incluido es ``verifier``; las claves de cada paquete son
    las publicadas; las cadenas íntegras con esas claves; recuentos y puntos de control como se
    declaran; y cada entrada, devuelta a columnas, igual a su fila de la base.
    """
    partition = snapshot.partition
    if contents.partition != partition or contents.manifest.get("period") != partition.period:
        raise ArchiveVerificationFailed(ArchiveFailure.FORMAT_MISMATCH, "partición")
    if contents.verifier != verifier:
        raise ArchiveVerificationFailed(ArchiveFailure.VERIFIER_MISMATCH)
    published = _key_map(keys)
    for organization_id, package in contents.package_keys.items():
        if package != published:
            raise ArchiveVerificationFailed(
                ArchiveFailure.CHECKPOINT_MISMATCH, f"claves de {organization_id}"
            )
    if [segment.organization_id for segment in contents.segments] != sorted(snapshot.rows):
        raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "organizaciones")
    _self_consistent(contents, lambda _organization: published)
    if contents.manifest.get("entry_count") != snapshot.entry_count:
        raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "recuento de la base")
    restored = restore_rows(contents)
    expected = [
        column_values(row)
        for organization_id in sorted(snapshot.rows)
        for row in snapshot.rows[organization_id]
    ]
    if len(restored) != len(expected):
        raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "filas")
    for got, want in zip(restored, expected, strict=True):
        if got != want:
            raise ArchiveVerificationFailed(ArchiveFailure.ENTRY_MISMATCH, str(want["entry_id"]))


def verify_download(
    data: bytes,
    uploaded_sha256: str,
    snapshot: PartitionSnapshot,
    keys: Sequence[CheckpointPublicKey],
    verifier: bytes,
) -> ArchiveContents:
    """Lo leído del almacén: el SHA-256 de lo subido y ``verify_archive``."""
    if hashlib.sha256(data).hexdigest() != uploaded_sha256:
        raise ArchiveVerificationFailed(ArchiveFailure.DIGEST_MISMATCH)
    contents = read_archive(data)
    verify_archive(contents, snapshot, keys, verifier)
    return contents


# --- Servicio -----------------------------------------------------------------------------------


@repository
class AuditArchiver:
    """Archiva las particiones de auditoría vencidas (PAT-NUC-MAN-03)."""

    def __init__(
        self,
        *,
        database: ArchiveDatabase,
        storage: StoragePort,
        writer: ArchiveRecordWriter,
        audit: ArchiveAudit,
        outbox: OutboxPort,
        checkpoint_keys: Callable[[], Sequence[CheckpointPublicKey]],
        verifier: bytes,
        clock: Clock,
        kms_key_id: str | None = None,
        online_months: int = AUDIT_ONLINE_MONTHS,
        batch_size: int = READ_BATCH,
        tables: Sequence[ArchivedTable] = ARCHIVED_TABLES,
    ) -> None:
        if not isinstance(verifier, bytes) or not verifier:
            raise ValueError("verifier debe ser el contenido de vigia_verify.py")
        if isinstance(online_months, bool) or not isinstance(online_months, int):
            raise TypeError("online_months debe ser int")
        if online_months < 1:
            raise ValueError("online_months debe ser al menos 1")
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size debe estar entre 1 y 10000")
        self._database = database
        self._storage = storage
        self._writer = writer
        self._audit = audit
        self._outbox = outbox
        self._checkpoint_keys = checkpoint_keys
        self._verifier = verifier
        self._clock = clock
        self._kms_key_id = kms_key_id
        self._online_months = online_months
        self._batch_size = batch_size
        # La lista ampliada (PAT-GOB-ESC-02): las tablas de volumen de fleet, cada una con su
        # plazo, en la misma pasada y con las mismas dependencias.
        self._tables = TableArchiver(
            database=database,
            storage=storage,
            writer=writer,
            clock=clock,
            kms_key_id=kms_key_id,
            tables=tables,
            batch_size=batch_size,
        )

    @property
    def tables(self) -> TableArchiver:
        return self._tables

    def cutoff(self) -> date:
        """Primer mes que sigue en línea: se archiva todo mes anterior a él."""
        return add_months(month_of(self._clock.now()), -self._online_months)

    async def due_partitions(self, context: ScopeContext) -> list[AuditPartition]:
        """Particiones adjuntas de ``shared.audit_entry`` cuyo mes es anterior a ``cutoff``."""
        async with self._database.transaction(context) as transaction:
            rows = (await transaction.execute(_ATTACHED_PARTITIONS)).all()
        cutoff = self.cutoff()
        partitions = [AuditPartition.from_name(str(row.name)) for row in rows]
        return sorted(p for p in partitions if p is not None and p.month < cutoff)

    async def snapshot(self, context: ScopeContext, partition: AuditPartition) -> PartitionSnapshot:
        """Lee la partición entera por lotes y la contrasta con su resumen en la base."""
        rows: dict[uuid.UUID, list[Mapping[str, Any]]] = {}
        async with self._database.transaction(context) as transaction:
            summary = (await transaction.execute(_SUMMARY, {"partition": partition.name})).all()
            after_organization: uuid.UUID | None = None
            after_sequence: int | None = None
            while True:
                batch = (
                    await transaction.execute(
                        _ROWS,
                        {
                            "partition": partition.name,
                            "after_organization": after_organization,
                            "after_sequence": after_sequence,
                            "max_rows": self._batch_size,
                        },
                    )
                ).all()
                for row in batch:
                    mapping = dict(row._mapping)
                    organization_id = uuid.UUID(str(mapping["organization_id"]))
                    rows.setdefault(organization_id, []).append(mapping)
                    after_organization = organization_id
                    after_sequence = int(mapping["chain_sequence"])
                if len(batch) < self._batch_size:
                    break
        expected = {
            uuid.UUID(str(row.organization_id)): (
                int(row.entries),
                int(row.first_sequence),
                int(row.last_sequence),
            )
            for row in summary
        }
        found = {
            organization_id: (
                len(organization_rows),
                int(organization_rows[0]["chain_sequence"]),
                int(organization_rows[-1]["chain_sequence"]),
            )
            for organization_id, organization_rows in rows.items()
        }
        if expected != found:
            raise ArchiveVerificationFailed(ArchiveFailure.SOURCE_BROKEN, "lectura incompleta")
        return PartitionSnapshot(partition, rows)

    async def archive(self, context: ScopeContext, partition: AuditPartition) -> ArchivedPartition:
        """Exporta, sube, lee de vuelta, verifica y solo entonces desprende y registra."""
        snapshot = await self.snapshot(context, partition)
        keys = tuple(self._checkpoint_keys())
        exported_at = self._clock.now()
        data = build_archive(snapshot, keys, self._verifier, exported_at)
        uploaded_sha256 = hashlib.sha256(data).hexdigest()
        key = partition.object_key
        await self._storage.put_object(key, data, ARCHIVE_CONTENT_TYPE, kms_key_id=self._kms_key_id)
        downloaded = await self._storage.get_object(key)
        verify_download(downloaded, uploaded_sha256, snapshot, keys, self._verifier)
        archived_at = self._clock.now()
        content = {
            "partition_name": partition.qualified_name,
            "period": partition.period,
            "archive_object_key": key,
            "archive_sha256": uploaded_sha256,
            "entry_count": snapshot.entry_count,
            "archived_at": format_timestamp(archived_at),
        }
        try:
            async with self._database.transaction(context) as transaction:
                await transaction.execute(
                    _DETACH,
                    {
                        "partition": partition.name,
                        "expected_entries": snapshot.entry_count,
                        "not_after": partition.range_end,
                    },
                )
                result = await self._writer.write(
                    context, ARCHIVED_RECORD_TYPE, content, transaction=transaction
                )
                if not isinstance(result, Receipt):
                    # Un rechazo del escritor: se revierte también el desprendimiento.
                    raise RuntimeError(f"audit_partition_archived rechazado: {result!r}")
        except sa_exc.DBAPIError as error:
            # La base rechazó el desprendimiento: la partición ya no es la verificada (entradas
            # nuevas o posteriores) o ya no está adjunta. Sigue como estaba y no hay registro.
            if _sqlstate(error) not in _DETACH_REJECTED:
                raise
            raise ArchiveVerificationFailed(
                ArchiveFailure.PARTITION_CHANGED, str(_sqlstate(error))
            ) from None
        _log.info(
            "partición de auditoría archivada y desprendida",
            partition=partition.qualified_name,
            entries=snapshot.entry_count,
        )
        return ArchivedPartition(partition, key, uploaded_sha256, snapshot.entry_count, archived_at)

    async def alert(
        self,
        context: ScopeContext,
        partition: AuditPartition | TablePartition,
        failure: ArchiveVerificationFailed,
    ) -> None:
        """``security_alert`` y la entrada ``integrity_verification`` con ``error``, confirmadas."""
        occurred_at = format_timestamp(self._clock.now())
        async with self._database.transaction(context) as transaction:
            await self._outbox.publish(
                transaction,
                NewEvent(
                    event_name=SECURITY_ALERT_EVENT,
                    payload={"alert_kind": SECURITY_ALERT_KIND, "occurred_at": occurred_at},
                ),
            )
            await self._audit.append(
                context,
                "integrity_verification",
                outcome="error",
                filters={
                    "task": ARCHIVE_AUDIT_PARTITIONS,
                    "check": ARCHIVE_CHECK,
                    "partition": partition.qualified_name,
                    "failure_reason": failure.reason.value,
                },
                result_count=0,
                transaction=transaction,
            )

    async def run(self, context: ScopeContext) -> ArchiveRunReport:
        """Una pasada: cada partición vencida por separado, primero la auditoría y después la
        lista ampliada de ``fleet``; ``AuditArchiveFailed`` si alguna quedó sin archivar, después
        de intentar las demás."""
        report = ArchiveRunReport()
        for partition in await self.due_partitions(context):
            await self._archive_one(context, partition, report)
        for table_partition in await self._tables.due_partitions(context):
            await self._archive_one(context, table_partition, report)
        if report.failed:
            raise AuditArchiveFailed([partition.qualified_name for partition, _ in report.failed])
        return report

    async def _archive_one(
        self,
        context: ScopeContext,
        partition: AuditPartition | TablePartition,
        report: ArchiveRunReport,
    ) -> None:
        """Archiva una partición y anota el resultado; nunca deja escapar su fallo."""
        try:
            if isinstance(partition, TablePartition):
                report.archived.append(await self._tables.archive(context, partition))
            else:
                report.archived.append(await self.archive(context, partition))
        except PartitionStillOpen as still_open:
            # Una alarma sigue abierta: su cierre tiene que poder escribirse. Sin alerta; la
            # pasada siguiente vuelve a mirarla.
            _log.warning(
                "partición vencida con filas abiertas: sigue adjunta",
                partition=still_open.partition.qualified_name,
                open_rows=still_open.open_rows,
            )
            report.deferred.append(still_open.partition)
        except ArchiveVerificationFailed as failure:
            _log.error(
                "archivo de auditoría no verificado: la partición sigue adjunta",
                partition=partition.qualified_name,
                failure_reason=failure.reason.value,
            )
            await self.alert(context, partition, failure)
            report.failed.append((partition, failure.reason.value))
        except (StorageUnavailable, TransientDatabaseError) as error:
            _log.warning(
                "archivado aplazado por un fallo transitorio",
                partition=partition.qualified_name,
                code=getattr(error, "code", type(error).__name__),
            )
            report.failed.append((partition, getattr(error, "code", "transient")))
        except Exception:
            # Un fallo no previsto no abandona las particiones siguientes; la pasada termina
            # fallida igualmente y la partición sigue adjunta (todo lo que la cambia va en una
            # transacción).
            _log.exception(
                "archivado fallido por un error no previsto",
                partition=partition.qualified_name,
            )
            report.failed.append((partition, "unexpected"))


# --- Restauración de solo lectura ---------------------------------------------------------------


async def restore_audit_partition(
    storage: StoragePort, object_key: str, expected_sha256: str
) -> RestoredPartition:
    """Descarga y verifica un archivo con sus propias claves; no escribe en la base.

    ``expected_sha256`` es el ``archive_sha256`` del registro ``audit_partition_archived``.
    """
    _require_sha256(expected_sha256)
    return restored_from_bytes(await storage.get_object(object_key), expected_sha256)


def _require_sha256(expected_sha256: object) -> None:
    if not isinstance(expected_sha256, str) or _HEX64.fullmatch(expected_sha256) is None:
        raise ValueError("expected_sha256 debe ser un SHA-256 hexadecimal en minúsculas")


def restored_from_bytes(data: bytes, expected_sha256: str) -> RestoredPartition:
    """``restore_audit_partition`` sobre los bytes ya descargados (``vigia-admin`` los verifica
    y después los extrae con ``extract_archive`` sin descargarlos otra vez)."""
    _require_sha256(expected_sha256)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ArchiveVerificationFailed(ArchiveFailure.DIGEST_MISMATCH)
    contents = read_archive(data)
    _self_consistent(contents, lambda organization_id: contents.package_keys[organization_id])
    return RestoredPartition(contents, restore_rows(contents))


def extract_archive(data: bytes, directory: Path) -> list[Path]:
    """Escribe los miembros de un archivo ya verificado en ``directory`` (que no debe existir).

    Solo los nombres que ``read_archive`` acepta (fijos o ``organizations/<uuid>/...``): ninguna
    ruta del archivo sale de ``directory``.
    """
    contents = read_archive(data)
    directory.mkdir(parents=False, exist_ok=False)
    written: list[Path] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [ARCHIVE_MANIFEST, VERIFIER_FILE]
        for segment in contents.segments:
            names += [f"{segment.directory}/manifest.json", f"{segment.directory}/{CHAIN_FILE}"]
        for name in names:
            target = directory.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_member(archive, name))
            written.append(target)
    return written


# --- Registro de la tarea -----------------------------------------------------------------------


def archive_audit_partitions_handler(
    archiver: AuditArchiver, provider_organization_id: uuid.UUID
) -> PeriodicHandler:
    """Manejador de ``archive_audit_partitions``: actúa solo en la iteración de la proveedora.

    No escribe en la transacción del planificador: cada paso abre la suya con el mismo contexto
    (lectura, desprendimiento con su registro, alerta), para que un fallo de una partición no
    deshaga lo ya confirmado de otra.
    """
    if type(provider_organization_id) is not uuid.UUID:
        raise TypeError("provider_organization_id debe ser uuid.UUID")

    async def handler(transaction: Transaction) -> None:
        context = transaction.context
        if context.organization_id != provider_organization_id:
            return
        await archiver.run(context)

    return handler


def register_archive_audit_partitions(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea mensual ``archive_audit_partitions`` de U-02 (§4.3)."""
    return registry.register(
        ARCHIVE_AUDIT_PARTITIONS, ARCHIVE_AUDIT_PARTITIONS_SCHEDULE, handler, unit=ActorUnit.U02
    )
