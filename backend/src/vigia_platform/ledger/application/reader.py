"""``LectorExpediente``: toda lectura del expediente y de la auditoría (LC-NUC-11; BR-NUC-59, 63).

Operaciones del puerto (business-logic-model §4 "Lectura" y §10.1):

- ``list(context, filters, page)``: registros de la organización del contexto con filtros por tipo,
  planta, zona, nodo e intervalo de ``received_at``, del más reciente al más antiguo, paginados
  **por clave** (``received_at, record_id`` del último elemento; nunca por desplazamiento) con
  página de 1 a 200 (PAT-NUC-ESC-07). Un registro que se inserte entre dos páginas tiene una
  marca de recepción posterior a la de la primera página, así que la página siguiente no repite
  ni omite nada.
- ``get(context, record_id)``: un registro, o ``None`` si no existe **o** está fuera del alcance
  (la interfaz responde ``not_found`` en los dos casos, nunca ``forbidden``).
- ``by_source(context, record_type, source_key)``: la idempotencia de U-03 (BR-NUC-48).
- ``list_audit(context, filters, page)``: las entradas de la cadena de auditoría de la
  organización, con la misma paginación por clave (``occurred_at, entry_id``).

**Alcance.** La seguridad a nivel de fila ya limita a la organización del contexto; el puerto
filtra además por ``allowed_scopes`` (BR-NUC-04, 13 a 16): un alcance de organización (de la
organización del contexto) ve todo; uno de planta, los registros de esa planta; uno de zona,
solo los de esa zona. Un contexto sin alcances no ve nada (falla cerrado). Qué **rol** puede leer
qué lo decide ``authorize`` en la ruta (TASK-125, TASK-137), no este puerto.

**Auditoría de cada lectura** (BR-NUC-59, 62): ``list`` y ``get`` escriben exactamente una
entrada ``ledger_read`` (filtros tal cual, en bytes canónicos de a lo sumo 4 KB, y
``result_count``) o ``ledger_detail_read`` (el identificador pedido) en **la misma transacción**
que la consulta (PAT-NUC-ESC-07): si la entrada no puede escribirse, la lectura no devuelve nada.
``list_audit`` escribe ``audit_read``. Los filtros se validan antes de abrir la transacción.

``by_source`` **no** escribe entrada ni filtra por alcance: es la comprobación de idempotencia del
camino de escritura de U-03, que por definición es de toda la organización (la unicidad de
``ledger.record_source_key``), y solo devuelve identificador, marca y ``content_hash``; nunca el
contenido.

**Nunca contenido de evidencias** (BR-NUC-64 a 66): de cada evidencia se devuelven sus
metadatos verificados (identificadores, tamaño, ``sha256``, tipo, intervalo), sin clave de
almacén ni URL; la URL de lectura se pide aparte (``EvidencePort``, TASK-121).

Una entrada inválida lanza ``LedgerQueryInvalid`` antes de tocar la base; sin contexto,
``ContextAbsent``. Los fallos de la base salen como en ``shared.db`` (``TemporarilyUnavailable``).
"""

from __future__ import annotations

import enum
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.audit_writer import (
    MAX_FILTERS_BYTES,
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import MAX_SOURCE_KEY_CHARS, LedgerDatabase
from vigia_platform.ledger.canonical import CanonicalFormError, canonical_bytes_sync
from vigia_platform.shared.context import ContextAbsent, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Transaction

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_FILTER_VALUES",
    "MAX_PAGE_SIZE",
    "AuditCursor",
    "AuditEntryView",
    "AuditFilters",
    "AuditPageRequest",
    "EvidenceReference",
    "LectorExpediente",
    "LedgerFilters",
    "LedgerQueryInvalid",
    "LedgerRecordView",
    "Page",
    "PageRequest",
    "RecordActor",
    "RecordCursor",
    "SourceMatch",
]

MAX_PAGE_SIZE: Final = 200
"""Página máxima de una lista (BR-NUC-49, PAT-NUC-ESC-07)."""

DEFAULT_PAGE_SIZE: Final = 50
"""Página por defecto ``[objetivo propio]``."""

MAX_FILTER_VALUES: Final = 32
"""Valores como mucho en un filtro de lista (tipos, operaciones): acota los filtros a 4 KB."""

RECORD_RESOURCE_KIND: Final = "ledger_record"
"""``resource_ref.kind`` de ``ledger_detail_read``."""

_SNAKE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LedgerQueryInvalid(ValueError):
    """Filtros, página o identificador inválidos: no se consulta ni se audita nada."""

    code: Final = "query_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"consulta del expediente inválida: {detail}")


# --- Validación de entradas ---------------------------------------------------------------------


def _require_context(context: object) -> ScopeContext:
    if not isinstance(context, ScopeContext):
        raise ContextAbsent()
    return context


def _plain(value: uuid.UUID | None) -> uuid.UUID | None:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia; la auditoría exige el tipo)."""
    return None if value is None else uuid.UUID(int=value.int)


def _id(value: uuid.UUID) -> uuid.UUID:
    return uuid.UUID(int=value.int)


def _uuid(value: object, name: str) -> uuid.UUID | None:
    if value is not None and not isinstance(value, uuid.UUID):
        raise LedgerQueryInvalid(f"{name} debe ser uuid.UUID")
    return _plain(value)


def _required_uuid(value: object, name: str) -> uuid.UUID:
    checked = _uuid(value, name)
    if checked is None:
        raise LedgerQueryInvalid(f"{name} es obligatorio")
    return checked


def _moment(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise LedgerQueryInvalid(f"{name} debe ser una marca con zona horaria")
    return value.astimezone(UTC)


def _codes(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise LedgerQueryInvalid(f"{name} debe ser una tupla")
    if len(values) > MAX_FILTER_VALUES:
        raise LedgerQueryInvalid(f"{name} admite como mucho {MAX_FILTER_VALUES} valores")
    for value in values:
        if type(value) is not str or _SNAKE.fullmatch(value) is None:
            raise LedgerQueryInvalid(f"{name} solo admite códigos snake_case")
    return values


def _interval(start: datetime | None, end: datetime | None, names: str) -> None:
    if start is not None and end is not None and start >= end:
        raise LedgerQueryInvalid(f"{names}: el inicio debe ser anterior al fin")


def _page_size(size: object) -> int:
    if type(size) is not int or not 1 <= size <= MAX_PAGE_SIZE:
        raise LedgerQueryInvalid(f"la página debe tener de 1 a {MAX_PAGE_SIZE} elementos")
    return size


def _stamp(moment: datetime) -> str:
    """Marca en UTC con microsegundos y ``Z``: el filtro auditado tal como se pidió."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def _audited(document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Los filtros tal como se auditarán; ``LedgerQueryInvalid`` si superan 4 KB (BR-NUC-62)."""
    try:
        size = len(canonical_bytes_sync(document))
    except CanonicalFormError:  # pragma: no cover - solo identificadores, códigos y marcas
        raise LedgerQueryInvalid("los filtros no son JSON canónico") from None
    if size > MAX_FILTERS_BYTES:
        raise LedgerQueryInvalid(f"los filtros superan {MAX_FILTERS_BYTES} B")
    return document


# --- Valores del puerto -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerFilters:
    """Filtros de ``list``; todos opcionales y combinados con Y.

    ``received_from`` es inclusivo y ``received_before`` exclusivo.
    """

    record_types: tuple[str, ...] = ()
    plant_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None
    received_from: datetime | None = None
    received_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecordCursor:
    """Clave del último registro de una página: la siguiente empieza justo después."""

    received_at: datetime
    record_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Tamaño de página (1 a 200) y clave del último registro de la página anterior."""

    size: int = DEFAULT_PAGE_SIZE
    after: RecordCursor | None = None


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """Filtros de ``list_audit``; ``occurred_from`` inclusivo y ``occurred_before`` exclusivo."""

    operations: tuple[str, ...] = ()
    actor_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    occurred_from: datetime | None = None
    occurred_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """Clave de la última entrada de una página de auditoría."""

    occurred_at: datetime
    entry_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AuditPageRequest:
    size: int = DEFAULT_PAGE_SIZE
    after: AuditCursor | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Una página y la clave para pedir la siguiente (``None`` si no hay más)."""

    items: tuple[T, ...]
    next_cursor: RecordCursor | AuditCursor | None


@dataclass(frozen=True, slots=True)
class RecordActor:
    """La instantánea del actor que escribió (BR-NUC-49)."""

    kind: str
    id: uuid.UUID
    display_name_snapshot: str
    role_in_use: str | None
    concession_id: uuid.UUID | None
    unit: str


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Referencia a una evidencia: metadatos verificados, nunca su contenido ni su URL."""

    evidence_id: uuid.UUID
    clip_id: uuid.UUID
    camera_id: uuid.UUID
    sha256: str
    size_bytes: int
    content_type: str
    media_kind: str
    duration_ms: int | None
    segment: str
    starts_at: datetime | None
    ends_at: datetime | None
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerRecordView:
    """Un registro del expediente tal como se persistió, con las referencias de sus evidencias.

    ``content`` son los bytes canónicos persistidos (los que hashea la cadena); ``document()``
    los interpreta como JSON.
    """

    record_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    chain_sequence: int
    record_type: str
    schema_version: int
    actor: RecordActor
    scope_plant_id: uuid.UUID | None
    scope_zone_id: uuid.UUID | None
    scope_node_id: uuid.UUID | None
    correlation_id: uuid.UUID
    received_at: datetime
    occurred_at: datetime | None
    source_key: str | None
    content: bytes
    content_hash: str
    previous_hash: str
    record_hash: str
    evidences: tuple[EvidenceReference, ...]

    def document(self) -> Any:
        return json.loads(self.content)

    @property
    def cursor(self) -> RecordCursor:
        return RecordCursor(self.received_at, self.record_id)


@dataclass(frozen=True, slots=True)
class SourceMatch:
    """Lo que ``by_source`` devuelve: identidad y hash del registro con esa clave."""

    record_id: uuid.UUID
    received_at: datetime
    content_hash: str


@dataclass(frozen=True, slots=True)
class AuditEntryView:
    """Una entrada de la cadena de auditoría (``filters`` ya interpretado como JSON)."""

    entry_id: uuid.UUID
    chain_sequence: int
    actor: RecordActor
    operation: str
    scope_plant_id: uuid.UUID | None
    scope_zone_id: uuid.UUID | None
    resource_kind: str | None
    resource_id: uuid.UUID | None
    filters: Any
    result_count: int | None
    outcome: str
    correlation_id: uuid.UUID
    occurred_at: datetime
    previous_hash: str
    entry_hash: str

    @property
    def cursor(self) -> AuditCursor:
        return AuditCursor(self.occurred_at, self.entry_id)


# --- Alcance ------------------------------------------------------------------------------------


class _Reach(enum.Enum):
    WHOLE = enum.auto()
    PARTIAL = enum.auto()


@dataclass(frozen=True, slots=True)
class _Scopes:
    """``allowed_scopes`` del contexto como parámetros de la consulta."""

    reach: _Reach
    plants: tuple[uuid.UUID, ...]
    zones: tuple[uuid.UUID, ...]

    @classmethod
    def of(cls, context: ScopeContext) -> _Scopes:
        whole = any(
            scope.scope_level is ScopeLevel.ORGANIZATION
            and scope.scope_id == context.organization_id
            for scope in context.allowed_scopes
        )
        return cls(
            reach=_Reach.WHOLE if whole else _Reach.PARTIAL,
            plants=_ids(context, ScopeLevel.PLANT),
            zones=_ids(context, ScopeLevel.ZONE),
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "whole_organization": self.reach is _Reach.WHOLE,
            "scope_plants": list(self.plants),
            "scope_zones": list(self.zones),
        }


def _ids(context: ScopeContext, level: ScopeLevel) -> tuple[uuid.UUID, ...]:
    return tuple(
        dict.fromkeys(s.scope_id for s in context.allowed_scopes if s.scope_level is level)
    )


# --- Sentencias -------------------------------------------------------------------------------

_LIST_RECORDS: Final = text(
    "SELECT r.record_id, r.organization_id, r.plant_id, r.chain_sequence, r.record_type,"
    " r.schema_version, r.actor_kind, r.actor_id, r.actor_display_name_snapshot,"
    " r.actor_role_in_use, r.actor_concession_id, r.actor_unit, r.scope_plant_id,"
    " r.scope_zone_id, r.scope_node_id, r.correlation_id, r.received_at, r.occurred_at,"
    " r.source_key, r.content, r.content_hash, r.previous_hash, r.record_hash"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR r.scope_plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR r.scope_zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " AND (CAST(:record_types AS text[]) IS NULL"
    " OR r.record_type = ANY(CAST(:record_types AS text[])))"
    " AND (CAST(:plant_id AS uuid) IS NULL OR r.scope_plant_id = CAST(:plant_id AS uuid))"
    " AND (CAST(:zone_id AS uuid) IS NULL OR r.scope_zone_id = CAST(:zone_id AS uuid))"
    " AND (CAST(:node_id AS uuid) IS NULL OR r.scope_node_id = CAST(:node_id AS uuid))"
    " AND (CAST(:received_from AS timestamptz) IS NULL"
    " OR r.received_at >= CAST(:received_from AS timestamptz))"
    " AND (CAST(:received_before AS timestamptz) IS NULL"
    " OR r.received_at < CAST(:received_before AS timestamptz))"
    " AND (CAST(:after_received_at AS timestamptz) IS NULL"
    " OR (r.received_at, r.record_id)"
    " < (CAST(:after_received_at AS timestamptz), CAST(:after_record_id AS uuid)))"
    " ORDER BY r.received_at DESC, r.record_id DESC"
    " LIMIT :limit"
)

_GET_RECORD: Final = text(
    "SELECT r.record_id, r.organization_id, r.plant_id, r.chain_sequence, r.record_type,"
    " r.schema_version, r.actor_kind, r.actor_id, r.actor_display_name_snapshot,"
    " r.actor_role_in_use, r.actor_concession_id, r.actor_unit, r.scope_plant_id,"
    " r.scope_zone_id, r.scope_node_id, r.correlation_id, r.received_at, r.occurred_at,"
    " r.source_key, r.content, r.content_hash, r.previous_hash, r.record_hash"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = :organization_id AND r.record_id = :record_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR r.scope_plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR r.scope_zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

_EVIDENCE_OF_RECORDS: Final = text(
    "SELECT e.record_id, e.evidence_id, e.clip_id, e.camera_id, e.sha256, e.size_bytes,"
    " e.content_type, e.media_kind, e.duration_ms, e.segment, e.starts_at, e.ends_at,"
    " e.verified_at"
    " FROM ledger.evidence AS e"
    " WHERE e.organization_id = :organization_id"
    " AND e.record_id = ANY(CAST(:record_ids AS uuid[]))"
    " ORDER BY e.record_id, e.starts_at NULLS LAST, e.clip_id, e.evidence_id"
)

_BY_SOURCE: Final = text(
    "SELECT r.record_id, r.received_at, r.content_hash FROM ledger.record_source_key AS k"
    " JOIN ledger.ledger_record AS r"
    " ON r.record_id = k.record_id AND r.received_at = k.received_at"
    " AND r.organization_id = k.organization_id"
    " WHERE k.organization_id = :organization_id AND k.record_type = :record_type"
    " AND k.source_key = :source_key"
)

_LIST_AUDIT: Final = text(
    "SELECT a.entry_id, a.chain_sequence, a.actor_kind, a.actor_id,"
    " a.actor_display_name_snapshot, a.actor_role_in_use, a.actor_concession_id, a.actor_unit,"
    " a.operation, a.scope_plant_id, a.scope_zone_id, a.resource_kind, a.resource_id,"
    " a.filters, a.result_count, a.outcome, a.correlation_id, a.occurred_at,"
    " a.previous_hash, a.entry_hash"
    " FROM shared.audit_entry AS a"
    " WHERE a.organization_id = :organization_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR a.scope_plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR a.scope_zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " AND (CAST(:operations AS text[]) IS NULL"
    " OR a.operation = ANY(CAST(:operations AS text[])))"
    " AND (CAST(:actor_id AS uuid) IS NULL OR a.actor_id = CAST(:actor_id AS uuid))"
    " AND (CAST(:plant_id AS uuid) IS NULL OR a.scope_plant_id = CAST(:plant_id AS uuid))"
    " AND (CAST(:zone_id AS uuid) IS NULL OR a.scope_zone_id = CAST(:zone_id AS uuid))"
    " AND (CAST(:occurred_from AS timestamptz) IS NULL"
    " OR a.occurred_at >= CAST(:occurred_from AS timestamptz))"
    " AND (CAST(:occurred_before AS timestamptz) IS NULL"
    " OR a.occurred_at < CAST(:occurred_before AS timestamptz))"
    " AND (CAST(:after_occurred_at AS timestamptz) IS NULL"
    " OR (a.occurred_at, a.entry_id)"
    " < (CAST(:after_occurred_at AS timestamptz), CAST(:after_entry_id AS uuid)))"
    " ORDER BY a.occurred_at DESC, a.entry_id DESC"
    " LIMIT :limit"
)


# --- Conversión de filas ------------------------------------------------------------------------


def _actor(row: Row[Any]) -> RecordActor:
    return RecordActor(
        kind=row.actor_kind,
        id=_id(row.actor_id),
        display_name_snapshot=row.actor_display_name_snapshot,
        role_in_use=row.actor_role_in_use,
        concession_id=_plain(row.actor_concession_id),
        unit=row.actor_unit,
    )


def _evidence(row: Row[Any]) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=_id(row.evidence_id),
        clip_id=_id(row.clip_id),
        camera_id=_id(row.camera_id),
        sha256=row.sha256,
        size_bytes=int(row.size_bytes),
        content_type=row.content_type,
        media_kind=row.media_kind,
        duration_ms=None if row.duration_ms is None else int(row.duration_ms),
        segment=row.segment,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        verified_at=row.verified_at,
    )


def _record(row: Row[Any], evidences: Iterable[EvidenceReference]) -> LedgerRecordView:
    return LedgerRecordView(
        record_id=_id(row.record_id),
        organization_id=_id(row.organization_id),
        plant_id=_plain(row.plant_id),
        chain_sequence=int(row.chain_sequence),
        record_type=row.record_type,
        schema_version=int(row.schema_version),
        actor=_actor(row),
        scope_plant_id=_plain(row.scope_plant_id),
        scope_zone_id=_plain(row.scope_zone_id),
        scope_node_id=_plain(row.scope_node_id),
        correlation_id=_id(row.correlation_id),
        received_at=row.received_at,
        occurred_at=row.occurred_at,
        source_key=row.source_key,
        content=bytes(row.content),
        content_hash=row.content_hash,
        previous_hash=row.previous_hash,
        record_hash=row.record_hash,
        evidences=tuple(evidences),
    )


def _audit_entry(row: Row[Any]) -> AuditEntryView:
    return AuditEntryView(
        entry_id=_id(row.entry_id),
        chain_sequence=int(row.chain_sequence),
        actor=_actor(row),
        operation=row.operation,
        scope_plant_id=_plain(row.scope_plant_id),
        scope_zone_id=_plain(row.scope_zone_id),
        resource_kind=row.resource_kind,
        resource_id=_plain(row.resource_id),
        filters=None if row.filters is None else json.loads(bytes(row.filters)),
        result_count=None if row.result_count is None else int(row.result_count),
        outcome=row.outcome,
        correlation_id=_id(row.correlation_id),
        occurred_at=row.occurred_at,
        previous_hash=row.previous_hash,
        entry_hash=row.entry_hash,
    )


# --- Consultas validadas ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RecordQuery:
    parameters: dict[str, Any]
    audited: dict[str, JsonValue]
    size: int
    plant_id: uuid.UUID | None
    zone_id: uuid.UUID | None


def _record_query(filters: object, page: object) -> _RecordQuery:
    if not isinstance(filters, LedgerFilters):
        raise LedgerQueryInvalid("filters debe ser LedgerFilters")
    if not isinstance(page, PageRequest):
        raise LedgerQueryInvalid("page debe ser PageRequest")
    size = _page_size(page.size)
    record_types = _codes(filters.record_types, "record_types")
    plant_id = _uuid(filters.plant_id, "plant_id")
    zone_id = _uuid(filters.zone_id, "zone_id")
    node_id = _uuid(filters.node_id, "node_id")
    received_from = _moment(filters.received_from, "received_from")
    received_before = _moment(filters.received_before, "received_before")
    _interval(received_from, received_before, "received_from/received_before")
    after = page.after
    after_received_at: datetime | None = None
    after_record_id: uuid.UUID | None = None
    if after is not None:
        if not isinstance(after, RecordCursor):
            raise LedgerQueryInvalid("page.after debe ser RecordCursor")
        after_received_at = _moment(after.received_at, "page.after.received_at")
        after_record_id = _required_uuid(after.record_id, "page.after.record_id")
    audited: dict[str, JsonValue] = {}
    if record_types:
        audited["record_types"] = list(record_types)
    for name, value in (("plant_id", plant_id), ("zone_id", zone_id), ("node_id", node_id)):
        if value is not None:
            audited[name] = str(value)
    for name, moment in (("received_from", received_from), ("received_before", received_before)):
        if moment is not None:
            audited[name] = _stamp(moment)
    audited["page_size"] = size
    if after_received_at is not None and after_record_id is not None:
        audited["after"] = {
            "received_at": _stamp(after_received_at),
            "record_id": str(after_record_id),
        }
    return _RecordQuery(
        parameters={
            "record_types": list(record_types) or None,
            "plant_id": plant_id,
            "zone_id": zone_id,
            "node_id": node_id,
            "received_from": received_from,
            "received_before": received_before,
            "after_received_at": after_received_at,
            "after_record_id": after_record_id,
            "limit": size + 1,
        },
        audited=_audited(audited),
        size=size,
        plant_id=plant_id,
        zone_id=zone_id,
    )


def _audit_query(filters: object, page: object) -> _RecordQuery:
    if not isinstance(filters, AuditFilters):
        raise LedgerQueryInvalid("filters debe ser AuditFilters")
    if not isinstance(page, AuditPageRequest):
        raise LedgerQueryInvalid("page debe ser AuditPageRequest")
    size = _page_size(page.size)
    operations = _codes(filters.operations, "operations")
    actor_id = _uuid(filters.actor_id, "actor_id")
    plant_id = _uuid(filters.plant_id, "plant_id")
    zone_id = _uuid(filters.zone_id, "zone_id")
    occurred_from = _moment(filters.occurred_from, "occurred_from")
    occurred_before = _moment(filters.occurred_before, "occurred_before")
    _interval(occurred_from, occurred_before, "occurred_from/occurred_before")
    after = page.after
    after_occurred_at: datetime | None = None
    after_entry_id: uuid.UUID | None = None
    if after is not None:
        if not isinstance(after, AuditCursor):
            raise LedgerQueryInvalid("page.after debe ser AuditCursor")
        after_occurred_at = _moment(after.occurred_at, "page.after.occurred_at")
        after_entry_id = _required_uuid(after.entry_id, "page.after.entry_id")
    audited: dict[str, JsonValue] = {}
    if operations:
        audited["operations"] = list(operations)
    for name, value in (("actor_id", actor_id), ("plant_id", plant_id), ("zone_id", zone_id)):
        if value is not None:
            audited[name] = str(value)
    for name, moment in (("occurred_from", occurred_from), ("occurred_before", occurred_before)):
        if moment is not None:
            audited[name] = _stamp(moment)
    audited["page_size"] = size
    if after_occurred_at is not None and after_entry_id is not None:
        audited["after"] = {
            "occurred_at": _stamp(after_occurred_at),
            "entry_id": str(after_entry_id),
        }
    return _RecordQuery(
        parameters={
            "operations": list(operations) or None,
            "actor_id": actor_id,
            "plant_id": plant_id,
            "zone_id": zone_id,
            "occurred_from": occurred_from,
            "occurred_before": occurred_before,
            "after_occurred_at": after_occurred_at,
            "after_entry_id": after_entry_id,
            "limit": size + 1,
        },
        audited=_audited(audited),
        size=size,
        plant_id=plant_id,
        zone_id=zone_id,
    )


# --- El puerto --------------------------------------------------------------------------------


class LectorExpediente:
    """El puerto ``LectorExpediente`` (business-logic-model §10.1) sobre PostgreSQL."""

    def __init__(self, *, database: LedgerDatabase, audit: AuditWriter) -> None:
        self._database = database
        self._audit = audit

    async def list(
        self,
        context: ScopeContext,
        filters: LedgerFilters | None = None,
        page: PageRequest | None = None,
    ) -> Page[LedgerRecordView]:
        """Una página de registros con alcance; escribe una entrada ``ledger_read``."""
        context = _require_context(context)
        query = _record_query(filters or LedgerFilters(), page or PageRequest())
        parameters = {
            **query.parameters,
            **_Scopes.of(context).parameters(),
            "organization_id": context.organization_id,
        }
        async with self._database.transaction(context) as transaction:
            rows = list((await transaction.execute(_LIST_RECORDS, parameters)).all())
            more = len(rows) > query.size
            rows = rows[: query.size]
            evidences = await self._evidences(
                transaction, context, [_id(row.record_id) for row in rows]
            )
            await self._audit.append(
                context,
                AuditOperation.LEDGER_READ,
                plant_id=query.plant_id,
                zone_id=query.zone_id,
                filters=query.audited,
                result_count=len(rows),
                transaction=transaction,
            )
        items = tuple(_record(row, evidences.get(_id(row.record_id), ())) for row in rows)
        return Page(items=items, next_cursor=items[-1].cursor if more and items else None)

    async def get(self, context: ScopeContext, record_id: uuid.UUID) -> LedgerRecordView | None:
        """El registro, o ``None`` si no existe o está fuera del alcance (``not_found``).

        Escribe una entrada ``ledger_detail_read`` con el identificador pedido y
        ``result_count`` 1 o 0.
        """
        context = _require_context(context)
        requested = _required_uuid(record_id, "record_id")
        parameters = {
            **_Scopes.of(context).parameters(),
            "organization_id": context.organization_id,
            "record_id": requested,
        }
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(_GET_RECORD, parameters)).first()
            evidences = (
                await self._evidences(transaction, context, [requested]) if row is not None else {}
            )
            await self._audit.append(
                context,
                AuditOperation.LEDGER_DETAIL_READ,
                plant_id=None if row is None else _plain(row.scope_plant_id),
                zone_id=None if row is None else _plain(row.scope_zone_id),
                resource=ResourceRef(RECORD_RESOURCE_KIND, requested),
                result_count=0 if row is None else 1,
                transaction=transaction,
            )
        if row is None:
            return None
        return _record(row, evidences.get(requested, ()))

    async def by_source(
        self, context: ScopeContext, record_type: str, source_key: str
    ) -> SourceMatch | None:
        """El registro de la organización con esa clave de idempotencia (BR-NUC-48), o ``None``.

        Sin filtro de alcance ni entrada de auditoría: ver el docstring del módulo.
        """
        context = _require_context(context)
        if type(record_type) is not str or _SNAKE.fullmatch(record_type) is None:
            raise LedgerQueryInvalid("record_type debe ser un código snake_case")
        if type(source_key) is not str or not 1 <= len(source_key) <= MAX_SOURCE_KEY_CHARS:
            raise LedgerQueryInvalid(
                f"source_key debe tener de 1 a {MAX_SOURCE_KEY_CHARS} caracteres"
            )
        rows = await self._database.read(
            context,
            _BY_SOURCE,
            {
                "organization_id": context.organization_id,
                "record_type": record_type,
                "source_key": source_key,
            },
        )
        if not rows:
            return None
        row = rows[0]
        return SourceMatch(
            record_id=_id(row.record_id), received_at=row.received_at, content_hash=row.content_hash
        )

    async def list_audit(
        self,
        context: ScopeContext,
        filters: AuditFilters | None = None,
        page: AuditPageRequest | None = None,
    ) -> Page[AuditEntryView]:
        """Una página de la cadena de auditoría con alcance; escribe una entrada ``audit_read``."""
        context = _require_context(context)
        query = _audit_query(filters or AuditFilters(), page or AuditPageRequest())
        parameters = {
            **query.parameters,
            **_Scopes.of(context).parameters(),
            "organization_id": context.organization_id,
        }
        async with self._database.transaction(context) as transaction:
            rows = list((await transaction.execute(_LIST_AUDIT, parameters)).all())
            more = len(rows) > query.size
            rows = rows[: query.size]
            await self._audit.append(
                context,
                AuditOperation.AUDIT_READ,
                plant_id=query.plant_id,
                zone_id=query.zone_id,
                filters=query.audited,
                result_count=len(rows),
                transaction=transaction,
            )
        items = tuple(_audit_entry(row) for row in rows)
        return Page(items=items, next_cursor=items[-1].cursor if more and items else None)

    @staticmethod
    async def _evidences(
        transaction: Transaction, context: ScopeContext, record_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, tuple[EvidenceReference, ...]]:
        if not record_ids:
            return {}
        rows = (
            await transaction.execute(
                _EVIDENCE_OF_RECORDS,
                {"organization_id": context.organization_id, "record_ids": list(record_ids)},
            )
        ).all()
        grouped: dict[uuid.UUID, list[EvidenceReference]] = {}
        for row in rows:
            grouped.setdefault(_id(row.record_id), []).append(_evidence(row))
        return {record_id: tuple(items) for record_id, items in grouped.items()}
