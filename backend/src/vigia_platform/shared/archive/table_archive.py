"""Archivado de las particiones de ``fleet`` por la lista ampliada de ``archive_audit_partitions``
(TASK-203, LC-GOB-21, PAT-GOB-ESC-02, PR-GOB-26).

``heartbeat_history``, ``enrollment_attempt`` y ``fleet_alarm`` están particionadas por mes
(gob_0018). Su retención se cumple como la de la auditoría (LC-NUC-33), **sin una octava tarea**:
la tarea mensual ``archive_audit_partitions`` (``audit_archive.AuditArchiver.run``) recorre,
después de la auditoría, las tablas de ``ARCHIVED_TABLES`` con su **plazo por tabla**:

- ``fleet.heartbeat_history``: 90 días en línea ``[estimación propia]``; una partición vence
  cuando su mes terminó hace al menos 90 días según el reloj inyectado.
- ``fleet.enrollment_attempt`` y ``fleet.fleet_alarm``: 24 meses en línea (NFR-GOB-39); vence la
  partición cuyo mes es anterior al mes en curso menos 24.

Por cada partición adjunta vencida:

1. **Exporta** la partición entera (todas las organizaciones, por lotes y en orden de clave;
   ``shared.vigia_table_partition_rows`` de gob_0018), cada fila como el texto de ``to_jsonb`` en
   UTC, y la contrasta con el recuento de ``shared.vigia_table_partition_summary``. Estas tablas
   no tienen cadena: no hay ``chain_walk``. Una partición de alarmas con alguna **abierta** no se
   archiva todavía (su cierre todavía tiene que poder escribirse): queda adjunta, sin alerta, y se
   vuelve a mirar en la pasada siguiente.
2. **Empaqueta** un ZIP (``archive.json`` con formato, tabla, partición, mes, recuento y SHA-256
   de las filas; ``rows.jsonl`` con una fila por línea, exactamente el texto de la base).
3. **Sube** el archivo a ``vigia-archive`` cifrado con la clave ``vigia-archive``, bajo un
   **prefijo por tabla**: ``fleet/<tabla>/<AAAA-MM>/<tabla>_AAAA_MM.zip``.
4. **Lo lee de vuelta** y lo **verifica**: mismo SHA-256 que lo subido, manifiesto y miembros
   exactos, SHA-256 y recuento de las filas los declarados, y **cada fila igual, byte a byte**, a
   la de la base.
5. Solo entonces, en **una** transacción: ``shared.vigia_detach_table_partition`` (recuento de
   nuevo bajo bloqueo exclusivo, ninguna fila posterior ni alarma abierta; desprende y deja la
   tabla desprendida de solo anexar) y el registro ``audit_partition_archived`` (versión 2, que
   admite los nombres de ``fleet``) en la cadena de la **organización proveedora**.

Si la verificación falla, la partición **sigue adjunta**, no se registra como archivada y
``AuditArchiver`` alerta como con la auditoría (``security_alert`` y la entrada
``integrity_verification`` con ``error``). ``restore_table_partition`` es la restauración de solo
lectura: descarga, comprueba el SHA-256 registrado y devuelve las filas; nunca escribe en la base.

No lee la hora del sistema.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.ledger.chain.package_verifier import parse_json
from vigia_platform.shared.archive.errors import ArchiveFailure, ArchiveVerificationFailed, sqlstate
from vigia_platform.shared.archive.partitions import add_months, month_of
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StoragePort

__all__ = [
    "ARCHIVED_TABLES",
    "ENROLLMENT_ATTEMPT",
    "FLEET_ALARM",
    "HEARTBEAT_HISTORY",
    "TABLE_ARCHIVE_FORMAT",
    "TABLE_ARCHIVE_FORMAT_VERSION",
    "ArchivedTable",
    "ArchivedTablePartition",
    "PartitionStillOpen",
    "RestoredTablePartition",
    "TableArchiveContents",
    "TableArchiver",
    "TablePartition",
    "TableSnapshot",
    "build_table_archive",
    "read_table_archive",
    "restore_table_partition",
    "restore_table_rows",
    "verify_table_archive",
    "verify_table_download",
]

_log = get_logger("shared.archive")

ARCHIVED_RECORD_TYPE: Final = "audit_partition_archived"
TABLE_ARCHIVE_FORMAT: Final = "vigia-table-archive"
TABLE_ARCHIVE_FORMAT_VERSION: Final = 1
ARCHIVE_CONTENT_TYPE: Final = "application/zip"
MANIFEST: Final = "archive.json"
ROWS_FILE: Final = "rows.jsonl"
MAX_MEMBER_BYTES: Final = 1024 * 1024 * 1024
"""Tope de un miembro del ZIP al leerlo (1 GiB), como en la auditoría."""
READ_BATCH: Final = 5_000
"""Filas por lote al exportar ``[objetivo propio]``."""

_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_MANIFEST_KEYS: Final = frozenset(
    {
        "format",
        "format_version",
        "table",
        "partition",
        "period",
        "exported_at",
        "row_count",
        "rows",
        "rows_sha256",
    }
)

_ATTACHED: Final = text(
    "SELECT child.relname AS name FROM pg_catalog.pg_inherits AS inheritance"
    " JOIN pg_catalog.pg_class AS child ON child.oid = inheritance.inhrelid"
    " WHERE inheritance.inhparent = CAST(:table_name AS regclass) ORDER BY child.relname"
)
_SUMMARY: Final = text(
    "SELECT row_count, open_rows FROM shared.vigia_table_partition_summary(:table_name, :partition)"
)
_ROWS: Final = text(
    "SELECT row_id, row_at, row_json FROM shared.vigia_table_partition_rows("
    ":table_name, :partition, :after_id, :after_at, :max_rows)"
)
_DETACH: Final = text(
    "SELECT shared.vigia_detach_table_partition(:table_name, :partition, :expected_rows,"
    " :not_after)"
)
_DETACH_REJECTED: Final = frozenset({"55000", "42P01"})
"""``object_not_in_prerequisite_state`` (no es la verificada) y ``undefined_table`` (ya no está
adjunta), como en la auditoría."""


# --- Tablas y particiones -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchivedTable:
    """Una tabla particionada de la lista ampliada, con su plazo en línea (días o meses)."""

    name: str
    online_days: int | None = None
    online_months: int | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"fleet\.[a-z_]{1,48}", self.name) is None:
            raise ValueError("name debe ser fleet.<tabla>")
        if (self.online_days is None) == (self.online_months is None):
            raise ValueError("un plazo en días o en meses, no los dos")
        for value in (self.online_days, self.online_months):
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ValueError("el plazo debe ser al menos 1")

    @property
    def short_name(self) -> str:
        return self.name.split(".", 1)[1]

    def is_due(self, month: date, now: datetime) -> bool:
        """Si la partición del mes ``month`` ya superó el plazo en línea en el instante ``now``."""
        if self.online_days is not None:
            end = add_months(month, 1)
            ended = datetime(end.year, end.month, 1, tzinfo=UTC)
            return ended <= now - timedelta(days=self.online_days)
        months = self.online_months or 0  # __post_init__: uno de los dos plazos existe
        return month < add_months(month_of(now), -months)


HEARTBEAT_HISTORY: Final = ArchivedTable("fleet.heartbeat_history", online_days=90)
"""90 días en línea (domain-entities §3.8, NFR-GOB-16) ``[estimación propia]``."""
ENROLLMENT_ATTEMPT: Final = ArchivedTable("fleet.enrollment_attempt", online_months=24)
"""24 meses en línea (NFR-GOB-39)."""
FLEET_ALARM: Final = ArchivedTable("fleet.fleet_alarm", online_months=24)
"""24 meses en línea (NFR-GOB-39)."""
ARCHIVED_TABLES: Final[tuple[ArchivedTable, ...]] = (
    HEARTBEAT_HISTORY,
    ENROLLMENT_ATTEMPT,
    FLEET_ALARM,
)
"""La lista ampliada de ``archive_audit_partitions`` (PAT-GOB-ESC-02)."""


@dataclass(frozen=True, slots=True)
class TablePartition:
    """Una partición mensual de una tabla archivable (``<tabla>_AAAA_MM``)."""

    table: ArchivedTable
    month: date

    def __post_init__(self) -> None:
        if not isinstance(self.month, date) or isinstance(self.month, datetime):
            raise TypeError("month debe ser una fecha")
        if self.month.day != 1:
            raise ValueError("month debe ser el primer día del mes")

    @classmethod
    def from_name(cls, table: ArchivedTable, name: str) -> TablePartition | None:
        pattern = re.escape(table.short_name) + r"_([0-9]{4})_(0[1-9]|1[0-2])"
        match = re.fullmatch(pattern, name) if isinstance(name, str) else None
        if match is None:
            return None
        return cls(table, date(int(match.group(1)), int(match.group(2)), 1))

    @property
    def name(self) -> str:
        return f"{self.table.short_name}_{self.month.year:04d}_{self.month.month:02d}"

    @property
    def qualified_name(self) -> str:
        return f"fleet.{self.name}"

    @property
    def period(self) -> str:
        return f"{self.month.year:04d}-{self.month.month:02d}"

    @property
    def range_end(self) -> datetime:
        end = add_months(self.month, 1)
        return datetime(end.year, end.month, 1, tzinfo=UTC)

    @property
    def object_key(self) -> str:
        """Prefijo por tabla en ``vigia-archive``."""
        return f"fleet/{self.table.short_name}/{self.period}/{self.name}.zip"


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    """La partición leída de la base: cada fila como el texto de ``to_jsonb``, en orden de
    clave."""

    partition: TablePartition
    rows: tuple[bytes, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class TableArchiveContents:
    """Un archivo leído: su manifiesto y sus filas tal como se escribieron."""

    manifest: dict[str, Any]
    rows: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class ArchivedTablePartition:
    """Una partición de ``fleet`` archivada, verificada y desprendida en esta pasada."""

    partition: TablePartition
    object_key: str
    sha256: str
    entry_count: int
    archived_at: datetime


@dataclass(frozen=True, slots=True)
class RestoredTablePartition:
    """La restauración de solo lectura: el archivo y sus filas como documentos."""

    contents: TableArchiveContents
    rows: tuple[dict[str, Any], ...]


class PartitionStillOpen(Exception):
    """La partición vencida tiene filas abiertas (una alarma sin cerrar): todavía no se archiva."""

    def __init__(self, partition: TablePartition, open_rows: int) -> None:
        super().__init__(f"{partition.qualified_name} tiene {open_rows} filas abiertas")
        self.partition = partition
        self.open_rows = open_rows


# --- Formato ------------------------------------------------------------------------------------


def _zip_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def _rows_bytes(rows: Sequence[bytes]) -> bytes:
    return b"".join(row + b"\n" for row in rows)


def build_table_archive(snapshot: TableSnapshot, exported_at: datetime) -> bytes:
    """El ZIP de una partición: manifiesto y una fila por línea, el texto exacto de la base."""
    partition = snapshot.partition
    rows = _rows_bytes(snapshot.rows)
    manifest = {
        "format": TABLE_ARCHIVE_FORMAT,
        "format_version": TABLE_ARCHIVE_FORMAT_VERSION,
        "table": partition.table.name,
        "partition": partition.qualified_name,
        "period": partition.period,
        "exported_at": format_timestamp(exported_at),
        "row_count": snapshot.row_count,
        "rows": ROWS_FILE,
        "rows_sha256": hashlib.sha256(rows).hexdigest(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        _zip_member(
            archive,
            MANIFEST,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n",
        )
        _zip_member(archive, ROWS_FILE, rows)
    return buffer.getvalue()


def _unreadable(detail: str) -> ArchiveVerificationFailed:
    return ArchiveVerificationFailed(ArchiveFailure.UNREADABLE, detail)


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise _unreadable(f"{name} supera el tope")
    return archive.read(info)


def _split_rows(data: bytes) -> tuple[bytes, ...]:
    if not data:
        return ()
    if not data.endswith(b"\n"):
        raise _unreadable("las filas no terminan en salto de línea")
    rows = tuple(data.split(b"\n")[:-1])
    for row in rows:
        if not isinstance(parse_json(row), dict):
            raise _unreadable("fila que no es un objeto JSON")
    return rows


def read_table_archive(data: bytes) -> TableArchiveContents:
    """Lee un archivo sin extraer nada: miembros y manifiesto exactos, JSON estricto, SHA-256 y
    recuento de las filas los declarados."""
    if not isinstance(data, bytes | bytearray):
        raise TypeError("data debe ser bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != {MANIFEST, ROWS_FILE}:
                raise _unreadable("miembros del archivo distintos de los declarados")
            manifest = parse_json(_member(archive, MANIFEST))
            if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
                raise _unreadable("archive.json mal formado")
            if (
                manifest["format"] != TABLE_ARCHIVE_FORMAT
                or manifest["format_version"] != TABLE_ARCHIVE_FORMAT_VERSION
                or manifest["rows"] != ROWS_FILE
            ):
                raise ArchiveVerificationFailed(ArchiveFailure.FORMAT_MISMATCH, "archive.json")
            raw = _member(archive, ROWS_FILE)
    except ArchiveVerificationFailed:
        raise
    except Exception as error:
        # ZIP corrupto, CRC, miembro ausente, JSON no válido o descompresión fallida.
        raise _unreadable(type(error).__name__) from None
    declared = manifest["rows_sha256"]
    if not isinstance(declared, str) or _HEX64.fullmatch(declared) is None:
        raise _unreadable("rows_sha256")
    if hashlib.sha256(raw).hexdigest() != declared:
        raise ArchiveVerificationFailed(ArchiveFailure.DIGEST_MISMATCH, ROWS_FILE)
    try:
        rows = _split_rows(raw)
    except ArchiveVerificationFailed:
        raise
    except Exception as error:
        raise _unreadable(type(error).__name__) from None
    count = manifest["row_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
        raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "recuento declarado")
    return TableArchiveContents(manifest, rows)


def verify_table_archive(contents: TableArchiveContents, snapshot: TableSnapshot) -> None:
    """Verificación de vuelta contra la base; ``ArchiveVerificationFailed`` al primer fallo."""
    partition = snapshot.partition
    manifest = contents.manifest
    if (
        manifest.get("table") != partition.table.name
        or manifest.get("partition") != partition.qualified_name
        or manifest.get("period") != partition.period
    ):
        raise ArchiveVerificationFailed(ArchiveFailure.FORMAT_MISMATCH, "partición")
    if len(contents.rows) != snapshot.row_count:
        raise ArchiveVerificationFailed(ArchiveFailure.COUNT_MISMATCH, "recuento de la base")
    for index, (got, want) in enumerate(zip(contents.rows, snapshot.rows, strict=True)):
        if got != want:
            raise ArchiveVerificationFailed(ArchiveFailure.ENTRY_MISMATCH, f"fila {index}")


def verify_table_download(
    data: bytes, uploaded_sha256: str, snapshot: TableSnapshot
) -> TableArchiveContents:
    """Lo leído del almacén: el SHA-256 de lo subido y ``verify_table_archive``."""
    if hashlib.sha256(data).hexdigest() != uploaded_sha256:
        raise ArchiveVerificationFailed(ArchiveFailure.DIGEST_MISMATCH)
    contents = read_table_archive(data)
    verify_table_archive(contents, snapshot)
    return contents


def restore_table_rows(contents: TableArchiveContents) -> tuple[dict[str, Any], ...]:
    """Las filas del archivo como documentos (``restaurar`` de PR-GOB-26), en orden de clave."""
    restored: list[dict[str, Any]] = []
    for row in contents.rows:
        document = parse_json(row)
        if not isinstance(document, dict):  # pragma: no cover - read_table_archive ya lo exige
            raise _unreadable("fila que no es un objeto JSON")
        restored.append(document)
    return tuple(restored)


def restored_table_from_bytes(data: bytes, expected_sha256: str) -> RestoredTablePartition:
    """Restauración de solo lectura sobre bytes ya descargados."""
    if not isinstance(expected_sha256, str) or _HEX64.fullmatch(expected_sha256) is None:
        raise ValueError("expected_sha256 debe ser un SHA-256 hexadecimal en minúsculas")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ArchiveVerificationFailed(ArchiveFailure.DIGEST_MISMATCH)
    contents = read_table_archive(data)
    return RestoredTablePartition(contents, restore_table_rows(contents))


async def restore_table_partition(
    storage: StoragePort, object_key: str, expected_sha256: str
) -> RestoredTablePartition:
    """Descarga y verifica un archivo de ``fleet``; no escribe en la base.

    ``expected_sha256`` es el ``archive_sha256`` del registro ``audit_partition_archived``.
    """
    if not isinstance(expected_sha256, str) or _HEX64.fullmatch(expected_sha256) is None:
        raise ValueError("expected_sha256 debe ser un SHA-256 hexadecimal en minúsculas")
    return restored_table_from_bytes(await storage.get_object(object_key), expected_sha256)


# --- Servicio -----------------------------------------------------------------------------------


class TableArchiveDatabase(Protocol):
    """``shared.db.Database``."""

    def transaction(self, context: ScopeContext) -> Any: ...


class TableArchiveRecordWriter(Protocol):
    """``EscritorExpediente.write`` con la transacción del llamador."""

    async def write(
        self,
        context: ScopeContext | None,
        record_type: str,
        content: Mapping[str, Any],
        *,
        transaction: Any = None,
    ) -> object: ...


@repository
class TableArchiver:
    """Archiva las particiones vencidas de las tablas de ``ARCHIVED_TABLES`` (PAT-GOB-ESC-02).

    Lo usa ``AuditArchiver.run``: ninguna tarea propia.
    """

    def __init__(
        self,
        *,
        database: TableArchiveDatabase,
        storage: StoragePort,
        writer: TableArchiveRecordWriter,
        clock: Clock,
        kms_key_id: str | None = None,
        tables: Sequence[ArchivedTable] = ARCHIVED_TABLES,
        batch_size: int = READ_BATCH,
    ) -> None:
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size debe estar entre 1 y 10000")
        names = [table.name for table in tables]
        if len(names) != len(set(names)) or not set(names) <= {t.name for t in ARCHIVED_TABLES}:
            raise ValueError("tables debe ser parte de ARCHIVED_TABLES, sin repetir")
        self._database = database
        self._storage = storage
        self._writer = writer
        self._clock = clock
        self._kms_key_id = kms_key_id
        self._tables = tuple(tables)
        self._batch_size = batch_size

    @property
    def tables(self) -> tuple[ArchivedTable, ...]:
        return self._tables

    async def due_partitions(self, context: ScopeContext) -> list[TablePartition]:
        """Particiones adjuntas vencidas según el plazo de cada tabla (sin la de por defecto)."""
        now = self._clock.now()
        due: list[TablePartition] = []
        async with self._database.transaction(context) as transaction:
            for table in self._tables:
                rows = (await transaction.execute(_ATTACHED, {"table_name": table.name})).all()
                for row in rows:
                    partition = TablePartition.from_name(table, str(row.name))
                    if partition is not None and table.is_due(partition.month, now):
                        due.append(partition)
        return due

    async def snapshot(self, context: ScopeContext, partition: TablePartition) -> TableSnapshot:
        """Lee la partición entera por lotes y la contrasta con su resumen en la base."""
        table_name = partition.table.name
        rows: list[bytes] = []
        async with self._database.transaction(context) as transaction:
            summary = (
                await transaction.execute(
                    _SUMMARY, {"table_name": table_name, "partition": partition.name}
                )
            ).one()
            if int(summary.open_rows):
                raise PartitionStillOpen(partition, int(summary.open_rows))
            after_id: uuid.UUID | None = None
            after_at: datetime | None = None
            while True:
                batch = (
                    await transaction.execute(
                        _ROWS,
                        {
                            "table_name": table_name,
                            "partition": partition.name,
                            "after_id": after_id,
                            "after_at": after_at,
                            "max_rows": self._batch_size,
                        },
                    )
                ).all()
                for row in batch:
                    rows.append(str(row.row_json).encode("utf-8"))
                    after_id = uuid.UUID(str(row.row_id))
                    after_at = row.row_at
                if len(batch) < self._batch_size:
                    break
        if len(rows) != int(summary.row_count):
            raise ArchiveVerificationFailed(ArchiveFailure.SOURCE_BROKEN, "lectura incompleta")
        return TableSnapshot(partition, tuple(rows))

    async def archive(
        self, context: ScopeContext, partition: TablePartition
    ) -> ArchivedTablePartition:
        """Exporta, sube, lee de vuelta, verifica y solo entonces desprende y registra."""
        snapshot = await self.snapshot(context, partition)
        data = build_table_archive(snapshot, self._clock.now())
        uploaded_sha256 = hashlib.sha256(data).hexdigest()
        key = partition.object_key
        await self._storage.put_object(key, data, ARCHIVE_CONTENT_TYPE, kms_key_id=self._kms_key_id)
        downloaded = await self._storage.get_object(key)
        verify_table_download(downloaded, uploaded_sha256, snapshot)
        archived_at = self._clock.now()
        content = {
            "partition_name": partition.qualified_name,
            "period": partition.period,
            "archive_object_key": key,
            "archive_sha256": uploaded_sha256,
            "entry_count": snapshot.row_count,
            "archived_at": format_timestamp(archived_at),
        }
        try:
            async with self._database.transaction(context) as transaction:
                await transaction.execute(
                    _DETACH,
                    {
                        "table_name": partition.table.name,
                        "partition": partition.name,
                        "expected_rows": snapshot.row_count,
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
            if sqlstate(error) not in _DETACH_REJECTED:
                raise
            raise ArchiveVerificationFailed(
                ArchiveFailure.PARTITION_CHANGED, str(sqlstate(error))
            ) from None
        _log.info(
            "partición de fleet archivada y desprendida",
            partition=partition.qualified_name,
            entries=snapshot.row_count,
        )
        return ArchivedTablePartition(
            partition, key, uploaded_sha256, snapshot.row_count, archived_at
        )
