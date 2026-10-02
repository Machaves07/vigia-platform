"""``GET /ledger/records``, ``GET /ledger/records/{record_id}`` y ``GET /audit/entries`` (§10.2).

**Permiso según el tipo consultado** (``business-logic-model.md`` §10.2): la ruta declara
``integrity.verify``, la clave más amplia de las dos (todo rol con ``findings.read`` la tiene; lo
comprueba ``tests/examples/test_ledger_routes.py``), y el manejador elige la vista:

- **vista de integridad** (``integrity.verify``): solo los tipos de la propia cadena
  (``INTEGRITY_RECORD_TYPES``: puntos de control y claves), que no contienen nada de un hallazgo.
  Es la de quien solo verifica (``administrator``, ``platform_operator``): un administrador nunca
  lee hallazgos ni evidencias (RF-PLA-11);
- **vista de hallazgos** (``findings.read``): cualquier tipo, con el contexto reducido a las
  asignaciones con esa clave.

En ``list``, si todos los tipos pedidos son de integridad, la vista de integridad; si no, la de
hallazgos (sin ``findings.read`` en ninguna asignación: ``not_found`` auditado). En ``get``, la de
hallazgos y, si el registro no está en ella, la de integridad restringida a sus tipos: un registro
de otro tipo fuera de la vista de hallazgos es ``not_found``, igual que uno inexistente.

``LectorExpediente`` audita cada lectura (``ledger_read``, ``ledger_detail_read``, ``audit_read``)
en la misma transacción. ``by_source`` (la idempotencia de U-03) **nunca** se expone: ninguna ruta
la llama ni con datos de la petición ni sin ellos (seguimiento de VIG-59; lo fija una prueba).

``GET /audit/entries`` (``audit.read``): ``administrator`` y ``plant_manager`` de su alcance y
``platform_operator`` en la proveedora (BR-NUC-63), con el contexto reducido a esas asignaciones.

Paginación por clave (página de 1 a 200): ``next_cursor`` trae los dos valores que la petición
siguiente pasa como ``after_received_at``/``after_record_id`` (``after_occurred_at``/
``after_entry_id`` en la auditoría). Un parámetro desconocido o repetido es ``invalid_request``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, JsonValue

from vigia_platform.identity.authz.authorize import narrowed
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    LedgerHttp,
    cursor_stamp,
    exact_query,
    ledger_http,
    narrowed_context,
    no_store,
    parse_instant,
    request_context,
)
from vigia_platform.ledger.application.reader import (
    MAX_FILTER_VALUES,
    MAX_PAGE_SIZE,
    AuditCursor,
    AuditEntryView,
    AuditFilters,
    AuditPageRequest,
    EvidenceReference,
    LedgerFilters,
    LedgerQueryInvalid,
    LedgerRecordView,
    PageRequest,
    RecordActor,
    RecordCursor,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "INTEGRITY_RECORD_TYPES",
    "AuditEntryOut",
    "LedgerRecordOut",
    "records_router",
]

INTEGRITY_RECORD_TYPES: Final = frozenset({"checkpoint", "key_rotated", "key_set_published"})
"""Tipos que ve la vista de integridad: puntos de control y claves públicas (sin hallazgos)."""

Services = Annotated[LedgerHttp, Depends(ledger_http)]
_SNAKE: Final = r"^[a-z][a-z0-9_]{0,63}$"
_PAGE: Final = Query(ge=1, le=MAX_PAGE_SIZE)
_RECORD_QUERY: Final = (
    "record_type",
    "plant_id",
    "zone_id",
    "node_id",
    "received_from",
    "received_before",
    "page_size",
    "after_received_at",
    "after_record_id",
)
_AUDIT_QUERY: Final = (
    "operation",
    "actor_id",
    "plant_id",
    "zone_id",
    "occurred_from",
    "occurred_before",
    "page_size",
    "after_occurred_at",
    "after_entry_id",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActorOut(_Strict):
    kind: str
    id: uuid.UUID
    display_name_snapshot: str
    role_in_use: str | None
    concession_id: uuid.UUID | None
    unit: str


class ScopeOut(_Strict):
    plant_id: uuid.UUID | None
    zone_id: uuid.UUID | None
    node_id: uuid.UUID | None = None


class EvidenceOut(_Strict):
    """Metadatos verificados de una evidencia: nunca su clave de almacén, su URL ni sus bytes."""

    evidence_id: uuid.UUID
    clip_id: uuid.UUID
    camera_id: uuid.UUID
    sha256: str
    size_bytes: int
    content_type: str
    media_kind: str
    duration_ms: int | None
    segment: str
    starts_at: str | None
    ends_at: str | None
    verified_at: str


class LedgerRecordOut(_Strict):
    record_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    chain_sequence: int
    record_type: str
    schema_version: int
    actor: ActorOut
    scope: ScopeOut
    correlation_id: uuid.UUID
    received_at: str
    occurred_at: str | None
    source_key: str | None
    content: JsonValue
    content_hash: str
    previous_hash: str
    record_hash: str
    evidences: tuple[EvidenceOut, ...]


class RecordCursorOut(_Strict):
    received_at: str
    record_id: uuid.UUID


class LedgerRecordPage(_Strict):
    items: tuple[LedgerRecordOut, ...]
    next_cursor: RecordCursorOut | None


class AuditEntryOut(_Strict):
    entry_id: uuid.UUID
    chain_sequence: int
    actor: ActorOut
    operation: str
    scope: ScopeOut
    resource_kind: str | None
    resource_id: uuid.UUID | None
    filters: JsonValue
    result_count: int | None
    outcome: str
    correlation_id: uuid.UUID
    occurred_at: str
    previous_hash: str
    entry_hash: str


class AuditCursorOut(_Strict):
    occurred_at: str
    entry_id: uuid.UUID


class AuditEntryPage(_Strict):
    items: tuple[AuditEntryOut, ...]
    next_cursor: AuditCursorOut | None


def _actor(actor: RecordActor) -> ActorOut:
    return ActorOut(
        kind=actor.kind,
        id=actor.id,
        display_name_snapshot=actor.display_name_snapshot,
        role_in_use=actor.role_in_use,
        concession_id=actor.concession_id,
        unit=actor.unit,
    )


def _stamp(moment: Any) -> str | None:
    return None if moment is None else format_timestamp(moment)


def _evidence(evidence: EvidenceReference) -> EvidenceOut:
    return EvidenceOut(
        evidence_id=evidence.evidence_id,
        clip_id=evidence.clip_id,
        camera_id=evidence.camera_id,
        sha256=evidence.sha256,
        size_bytes=evidence.size_bytes,
        content_type=evidence.content_type,
        media_kind=evidence.media_kind,
        duration_ms=evidence.duration_ms,
        segment=evidence.segment,
        starts_at=_stamp(evidence.starts_at),
        ends_at=_stamp(evidence.ends_at),
        verified_at=format_timestamp(evidence.verified_at),
    )


def record_out(record: LedgerRecordView) -> LedgerRecordOut:
    return LedgerRecordOut(
        record_id=record.record_id,
        organization_id=record.organization_id,
        plant_id=record.plant_id,
        chain_sequence=record.chain_sequence,
        record_type=record.record_type,
        schema_version=record.schema_version,
        actor=_actor(record.actor),
        scope=ScopeOut(
            plant_id=record.scope_plant_id,
            zone_id=record.scope_zone_id,
            node_id=record.scope_node_id,
        ),
        correlation_id=record.correlation_id,
        received_at=format_timestamp(record.received_at),
        occurred_at=_stamp(record.occurred_at),
        source_key=record.source_key,
        content=record.document(),
        content_hash=record.content_hash,
        previous_hash=record.previous_hash,
        record_hash=record.record_hash,
        evidences=tuple(_evidence(e) for e in record.evidences),
    )


def _audit_out(entry: AuditEntryView) -> AuditEntryOut:
    return AuditEntryOut(
        entry_id=entry.entry_id,
        chain_sequence=entry.chain_sequence,
        actor=_actor(entry.actor),
        operation=entry.operation,
        scope=ScopeOut(plant_id=entry.scope_plant_id, zone_id=entry.scope_zone_id),
        resource_kind=entry.resource_kind,
        resource_id=entry.resource_id,
        filters=entry.filters,
        result_count=entry.result_count,
        outcome=entry.outcome,
        correlation_id=entry.correlation_id,
        occurred_at=format_timestamp(entry.occurred_at),
        previous_hash=entry.previous_hash,
        entry_hash=entry.entry_hash,
    )


def _optional_instant(value: str | None) -> Any:
    return None if value is None else parse_instant(value)


def _cursor_pair(moment: str | None, identifier: uuid.UUID | None) -> tuple[Any, Any] | None:
    """Los dos valores de la clave van juntos o no van."""
    if (moment is None) != (identifier is None):
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    if moment is None or identifier is None:
        return None
    return parse_instant(moment), identifier


def _findings_keys_cover_integrity() -> bool:
    """Todo rol con ``findings.read`` tiene ``integrity.verify``: la clave de la ruta es la más
    amplia de las dos y la vista de hallazgos nunca queda detrás de una denegación de ruta."""
    return all(
        PermissionKey.INTEGRITY_VERIFY in keys
        for keys in MATRIX.values()
        if PermissionKey.FINDINGS_READ in keys
    )


def records_router() -> APIRouter:
    if not _findings_keys_cover_integrity():  # pragma: no cover - lo vigila la matriz
        raise RuntimeError("findings.read sin integrity.verify: revisa la ruta del expediente")
    router = APIRouter(tags=["expediente"])
    route_key = PermissionKey.INTEGRITY_VERIFY.value

    @router.get(
        "/ledger/records",
        dependencies=[
            requires(route_key),
            Depends(exact_query(*_RECORD_QUERY[1:], repeatable=("record_type",))),
        ],
        summary="Registros del expediente con alcance, paginados por clave (lectura auditada)",
    )
    async def list_records(
        request: Request,
        response: Response,
        services: Services,
        record_type: Annotated[
            list[Annotated[str, Query(pattern=_SNAKE)]] | None,
            Query(max_length=MAX_FILTER_VALUES),
        ] = None,
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
        node_id: uuid.UUID | None = None,
        received_from: str | None = None,
        received_before: str | None = None,
        page_size: Annotated[int, _PAGE] = 50,
        after_received_at: str | None = None,
        after_record_id: uuid.UUID | None = None,
    ) -> LedgerRecordPage:
        no_store(response)
        context = request_context(request)
        types = tuple(dict.fromkeys(record_type or ()))
        integrity_only = bool(types) and set(types) <= INTEGRITY_RECORD_TYPES
        key = PermissionKey.INTEGRITY_VERIFY if integrity_only else PermissionKey.FINDINGS_READ
        reader_context = await narrowed_context(services, context, key)
        after = _cursor_pair(after_received_at, after_record_id)
        filters = LedgerFilters(
            record_types=types,
            plant_id=plant_id,
            zone_id=zone_id,
            node_id=node_id,
            received_from=_optional_instant(received_from),
            received_before=_optional_instant(received_before),
        )
        page = PageRequest(size=page_size, after=None if after is None else RecordCursor(*after))
        try:
            result = await services.reader.list(reader_context, filters, page)
        except LedgerQueryInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        cursor = result.next_cursor
        return LedgerRecordPage(
            items=tuple(record_out(item) for item in result.items),
            next_cursor=None
            if cursor is None
            else RecordCursorOut(
                received_at=cursor_stamp(cursor.received_at), record_id=cursor.record_id
            ),
        )

    @router.get(
        "/ledger/records/{record_id}",
        dependencies=[requires(route_key), Depends(exact_query())],
        summary="Un registro del expediente (lectura auditada); fuera de alcance, not_found",
    )
    async def get_record(
        record_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> LedgerRecordOut:
        no_store(response)
        context = request_context(request)
        provider = services.provider_organization_id
        findings = narrowed(context, PermissionKey.FINDINGS_READ, provider_organization_id=provider)
        integrity = narrowed(
            context, PermissionKey.INTEGRITY_VERIFY, provider_organization_id=provider
        )
        record: LedgerRecordView | None = None
        if findings is not None:
            record = await services.reader.get(findings, record_id)
        if (
            record is None
            and integrity is not None
            and (findings is None or set(integrity.allowed_scopes) != set(findings.allowed_scopes))
        ):
            record = await services.reader.get(
                integrity, record_id, record_types=tuple(sorted(INTEGRITY_RECORD_TYPES))
            )
        if record is None:
            raise ApiError(ApiErrorCode.NOT_FOUND)
        return record_out(record)

    @router.get(
        "/audit/entries",
        dependencies=[
            requires(PermissionKey.AUDIT_READ.value),
            Depends(exact_query(*_AUDIT_QUERY[1:], repeatable=("operation",))),
        ],
        summary="Entradas de la cadena de auditoría con alcance (lectura auditada)",
    )
    async def list_audit_entries(
        request: Request,
        response: Response,
        services: Services,
        operation: Annotated[
            list[Annotated[str, Query(pattern=_SNAKE)]] | None,
            Query(max_length=MAX_FILTER_VALUES),
        ] = None,
        actor_id: uuid.UUID | None = None,
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
        occurred_from: str | None = None,
        occurred_before: str | None = None,
        page_size: Annotated[int, _PAGE] = 50,
        after_occurred_at: str | None = None,
        after_entry_id: uuid.UUID | None = None,
    ) -> AuditEntryPage:
        no_store(response)
        context = request_context(request)
        reader_context = await narrowed_context(services, context, PermissionKey.AUDIT_READ)
        after = _cursor_pair(after_occurred_at, after_entry_id)
        filters = AuditFilters(
            operations=tuple(dict.fromkeys(operation or ())),
            actor_id=actor_id,
            plant_id=plant_id,
            zone_id=zone_id,
            occurred_from=_optional_instant(occurred_from),
            occurred_before=_optional_instant(occurred_before),
        )
        page = AuditPageRequest(
            size=page_size, after=None if after is None else AuditCursor(*after)
        )
        try:
            result = await services.reader.list_audit(reader_context, filters, page)
        except LedgerQueryInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        cursor = result.next_cursor
        return AuditEntryPage(
            items=tuple(_audit_out(item) for item in result.items),
            next_cursor=None
            if cursor is None
            else AuditCursorOut(
                occurred_at=cursor_stamp(cursor.occurred_at), entry_id=cursor.entry_id
            ),
        )

    return router
