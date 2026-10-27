"""La bandeja del lazo (SUP-U05-01; LC-LAZ §6.3; LC-LAZ-05).

``GET /loop/findings`` (``findings.read``, zona) con los **únicos** filtros de BR-LAZ-02:
``zone_id`` (repetible, hasta 50), ``family``, ``tier``, ``lifecycle_state`` (repetible, uno de los
siete estados activos), ``from`` y ``to`` (periodo ``[from, to)`` sobre la marca agregada), y
``order`` (``newest_first`` por defecto u ``oldest_first``), ``cursor`` y ``limit`` (1 a 200).
Responde ``{items, zones, partitions, next_cursor}``:

- ``items``: los elementos, con sus avisos (acuerdo revocado, acta no vigente, evidencia marcada,
  tramos faltantes) y, en zona unipersonal, todas sus marcas agregadas (``time_aggregated``);
- ``zones``: **cada** zona consultada con su aviso cerrado (``coverage_unavailable``,
  ``not_observed`` o ``no_node_assigned``), su nodo y su ``communication_state``, haya o no
  elementos: la bandeja nunca dice «sin hallazgos» (P2);
- ``partitions``: las plantas cuya proyección se está reconstruyendo o difiere del expediente
  (``projection_rebuilding``, ``projection_diverged``; BR-LAZ-156).

Una zona inexistente, de otra organización o fuera del alcance responde ``not_found`` (nunca
``forbidden``); un parámetro desconocido, repetido donde no se admite o con valor fuera de su
lista, ``invalid_request``. ``Cache-Control: no-store``. Sin auditoría de consulta (BR-LAZ-09).

El detalle y el historial (SUP-U05-02 y 04; LC-LAZ-06; ``loop.application.detail``), los dos con
``findings.read`` sobre la zona del hallazgo y sin parámetros de consulta:

- ``GET /loop/findings/{finding_state_id}``: el hallazgo con su cita del estándar (texto y
  ``sha256``, o la sección degradada ``standard_text_unavailable``), la compuerta de uso del
  instante, la cobertura del instante, los nodos, el episodio, los registros del expediente
  leídos (auditados) con sus señales y clips, las clasificaciones, las acciones, los análisis y
  sus avisos (``FindingDetailNotice``). Sin compuerta legible, **503 ``temporarily_unavailable``
  con ``loop_gate_state_unavailable``** y ``Retry-After``: el cuerpo es solo el error, ningún
  campo del hallazgo (fallo cerrado, BR-LAZ-12);
- ``GET /loop/findings/{finding_state_id}/classifications``: todas las clasificaciones del sujeto
  en orden, con motivo, categoría, autor y fecha; si el sujeto es una detección ya resuelta, también
  su resolución de la cola (``review_resolutions``), aparte porque no es una clasificación.

Inexistente, de otra organización o fuera del alcance: ``not_found``. En zona unipersonal toda
marca es el inicio de su ventana (``time_aggregated``).
"""

from __future__ import annotations

import base64
import binascii
import enum
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from vigia_contracts.models.enumerations import EvidenceTier, PredicateFamily

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    cursor_stamp,
    exact_query,
    no_store,
    parse_instant,
)
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.adapters.postgres.inbox_query import (
    INBOX_STATES,
    InboxCursor,
    InboxFilters,
)
from vigia_platform.loop.application.detail import (
    ClassificationHistory,
    ClassificationView,
    FindingDetail,
    FindingGateUnavailable,
    RecordView,
)
from vigia_platform.loop.application.inbox import (
    MAX_INBOX_PAGE,
    InboxItem,
    InboxPage,
    InboxRequestInvalid,
    ZoneDeclaration,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.domain.enums import (
    FindingDetailNotice,
    FindingLifecycleState,
    InboxFindingNotice,
    InboxPartitionNotice,
    InboxZoneNotice,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CLASSIFICATIONS_PATH",
    "DETAIL_PATH",
    "MAX_ZONES",
    "PATH",
    "ClassificationHistoryOut",
    "FindingDetailOut",
    "InboxOrder",
    "InboxPageOut",
    "decode_inbox_cursor",
    "detail_view",
    "encode_inbox_cursor",
    "findings_router",
    "history_view",
]

PATH: Final = "/loop/findings"
DETAIL_PATH: Final = "/loop/findings/{finding_state_id}"
CLASSIFICATIONS_PATH: Final = "/loop/findings/{finding_state_id}/classifications"
_GATE_UNAVAILABLE: Final = LoopDetailCode.GATE_STATE_UNAVAILABLE
MAX_ZONES: Final = 50
"""Zonas por consulta (``zone_id`` repetible)."""
MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TIMESTAMP_CHARS: Final = 40
_READ: Final = PermissionKey.FINDINGS_READ.value

Services = Annotated[LoopHttp, Depends(loop_http)]


class InboxOrder(enum.StrEnum):
    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FindingNoticeOut(_Strict):
    code: InboxFindingNotice
    since: str | None
    """Desde cuándo el acuerdo de uso está revocado (agregada en zona unipersonal)."""
    cause: str | None
    """La causa del acta no vigente (``catalog_change`` o ``model_version_change``)."""


class InboxStandardOut(_Strict):
    standard_id: uuid.UUID
    version: int
    """La versión con la que se evaluó, para siempre (H-16)."""


class InboxItemOut(_Strict):
    finding_state_id: uuid.UUID
    subject_kind: str
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    tier: str
    standard: InboxStandardOut
    lifecycle_state: FindingLifecycleState
    classification_outcome: str | None
    classification_count: int
    age_days: int
    observability_state_at_event: str
    occurred_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    due_at: str | None
    overdue_days: int
    time_aggregated: bool
    aggregation_window_minutes: int | None
    evidence_flagged: bool
    notices: tuple[FindingNoticeOut, ...]


class ZoneOut(_Strict):
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    notice: InboxZoneNotice
    node_id: uuid.UUID | None
    node_code: str | None
    communication_state: str | None


class PartitionOut(_Strict):
    plant_id: uuid.UUID
    notice: InboxPartitionNotice


class InboxPageOut(_Strict):
    items: tuple[InboxItemOut, ...]
    zones: tuple[ZoneOut, ...]
    partitions: tuple[PartitionOut, ...]
    next_cursor: str | None
    """Pásalo como ``cursor`` para la página siguiente; ``null`` si no hay más."""


def encode_inbox_cursor(cursor: InboxCursor) -> str:
    """Cursor opaco: la marca del cubo con microsegundos y el ``finding_state_id``."""
    raw = f"{cursor_stamp(cursor.occurred_at_bucket)}|{cursor.finding_state_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def decode_inbox_cursor(value: str) -> InboxCursor:
    """El cursor de ``encode_inbox_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, separator, identifier = raw.partition("|")
        if not separator:
            raise ValueError("cursor sin separador")
        bucket = parse_instant(moment)
        finding_state_id = uuid.UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    return InboxCursor(bucket, finding_state_id)


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else format_timestamp(moment.astimezone(UTC))


def item_view(item: InboxItem) -> InboxItemOut:
    return InboxItemOut(
        finding_state_id=item.finding_state_id,
        subject_kind=item.subject_kind,
        plant_id=item.plant_id,
        zone_id=item.zone_id,
        family=item.family,
        tier=item.tier,
        standard=InboxStandardOut(standard_id=item.standard_id, version=item.standard_version),
        lifecycle_state=item.lifecycle_state,
        classification_outcome=item.classification_outcome,
        classification_count=item.classification_count,
        age_days=item.age_days,
        observability_state_at_event=item.observability_state_at_event,
        occurred_at=format_timestamp(item.occurred_at),
        due_at=_stamp(item.due_at),
        overdue_days=item.overdue_days,
        time_aggregated=item.time_aggregated,
        aggregation_window_minutes=item.aggregation_window_minutes,
        evidence_flagged=item.evidence_flagged,
        notices=tuple(
            FindingNoticeOut(code=notice.code, since=_stamp(notice.since), cause=notice.cause)
            for notice in item.notices
        ),
    )


def zone_view(zone: ZoneDeclaration) -> ZoneOut:
    return ZoneOut(
        plant_id=zone.plant_id,
        zone_id=zone.zone_id,
        notice=zone.notice,
        node_id=zone.node_id,
        node_code=zone.node_code,
        communication_state=zone.communication_state,
    )


def page_view(page: InboxPage) -> InboxPageOut:
    return InboxPageOut(
        items=tuple(item_view(item) for item in page.items),
        zones=tuple(zone_view(zone) for zone in page.zones),
        partitions=tuple(
            PartitionOut(plant_id=partition.plant_id, notice=partition.notice)
            for partition in page.partitions
        ),
        next_cursor=None if page.next_cursor is None else encode_inbox_cursor(page.next_cursor),
    )


# --- detalle e historial (SUP-U05-02 y 04) ------------------------------------------------------


class DetailNoticeOut(_Strict):
    code: FindingDetailNotice
    since: str | None
    """Desde cuándo (acuerdo revocado, versión posterior del estándar)."""
    cause: str | None
    """La causa del acta no vigente."""


class StandardCitationOut(_Strict):
    standard_id: uuid.UUID
    version: int
    """La versión con la que se evaluó, para siempre (H-16; nunca se reevalúa)."""
    title_es: str | None
    declared_text: str | None
    """``null`` si el catálogo no lo dio: aviso ``standard_text_unavailable``."""
    declared_text_sha256: str | None
    effective_from: str | None
    superseded_since: str | None
    """Desde cuándo rige una versión posterior del catálogo (BR-LAZ-152)."""
    superseded_in_catalog_version: int | None


class GateAtEventOut(_Strict):
    status: str
    effective_from: str
    effective_until: str | None


class CoverageAtEventOut(_Strict):
    state: str
    causes: tuple[str, ...]
    layer: str


class NodeCameraOut(_Strict):
    camera_id: uuid.UUID
    code: str | None
    connected: bool
    observability_state: str


class NodeOut(_Strict):
    node_id: uuid.UUID
    code: str
    communication_state: str
    since: str
    last_heartbeat_at: str | None
    cameras: tuple[NodeCameraOut, ...]


class SignalOut(_Strict):
    signal_id: uuid.UUID | None
    role: str
    value: str
    valid: bool
    read_at: str | None


class ClipOut(_Strict):
    evidence_id: uuid.UUID
    clip_id: uuid.UUID
    camera_id: uuid.UUID
    segment: str
    media_kind: str
    content_type: str
    size_bytes: int
    sha256: str
    duration_ms: int | None
    starts_at: str | None
    ends_at: str | None
    read_url: str | None
    """De solo lectura, vigente a lo sumo 5 minutos; ``null`` si el almacén no la concedió."""


class RecordOut(_Strict):
    record_id: uuid.UUID
    record_type: str
    chain_sequence: int
    content_hash: str
    segment_index: int | None
    received_at: str
    occurred_at: str | None
    signals: tuple[SignalOut, ...]
    clips: tuple[ClipOut, ...]


class EpisodeOut(_Strict):
    episode_id: uuid.UUID
    segment_record_ids: tuple[uuid.UUID, ...]
    segment_count: int
    expected_segment_count: int
    missing_segment_indexes: tuple[int, ...]
    started_at: str
    ended_at: str
    total_episode_duration_ms: int
    total_condition_duration_ms: int
    continues_open: bool


class ClassifiedByOut(_Strict):
    """El usuario de la plataforma que clasificó (P8), nunca una persona observada."""

    user_id: uuid.UUID
    display_name: str
    role_in_use: str


class ClassificationItemOut(_Strict):
    classification_id: uuid.UUID
    record_id: uuid.UUID
    sequence_in_subject: int
    outcome: str
    reason_category: str
    reason_text: str
    is_reclassification: bool
    reinforced_reason: bool
    previous_outcome: str | None
    effect_on_loop: str
    classified_at: str
    classified_by: ClassifiedByOut


class ActionOut(_Strict):
    action_id: uuid.UUID
    record_id: uuid.UUID
    action_type: str
    action_description: str
    expected_outcome: str | None
    state: str
    assigned_at: str
    due_at: str
    counts_as_overdue: bool
    root_cause_analysis_id: uuid.UUID | None


class AnalysisOut(_Strict):
    analysis_id: uuid.UUID
    record_id: uuid.UUID | None
    state: str
    root_cause_category: str | None
    root_cause_statement: str | None
    conclusion: str | None
    signed_at: str | None
    created_at: str


class FindingDetailOut(_Strict):
    finding_state_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    tier: str
    lifecycle_state: FindingLifecycleState
    classification_outcome: str | None
    classification_count: int
    age_days: int
    observability_state_at_event: str
    occurred_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    due_at: str | None
    overdue_days: int
    time_aggregated: bool
    aggregation_window_minutes: int | None
    evidence_flagged: bool
    standard: StandardCitationOut
    gate_at_event: GateAtEventOut
    coverage_at_event: CoverageAtEventOut | None
    """``null`` si ``CoveragePort`` no respondió: aviso ``coverage_unavailable``."""
    nodes: tuple[NodeOut, ...] | None
    """``null`` si ``FleetQueryPort`` no respondió: aviso ``node_state_unavailable``."""
    episode: EpisodeOut | None
    records: tuple[RecordOut, ...]
    classifications: tuple[ClassificationItemOut, ...]
    actions: tuple[ActionOut, ...]
    analyses: tuple[AnalysisOut, ...]
    notices: tuple[DetailNoticeOut, ...]


class ReviewResolutionItemOut(_Strict):
    """La resolución de la detección en la cola (T05 o T06): no es una clasificación."""

    resolution_id: uuid.UUID
    record_id: uuid.UUID
    outcome: str
    reason_category: str
    reason_text: str
    resolved_at: str
    resolved_by: ClassifiedByOut


class ClassificationHistoryOut(_Strict):
    finding_state_id: uuid.UUID
    time_aggregated: bool
    aggregation_window_minutes: int | None
    classifications: tuple[ClassificationItemOut, ...]
    review_resolutions: tuple[ReviewResolutionItemOut, ...]
    """La resolución de la cola si el sujeto es una detección ya resuelta; vacía si no."""


def _classification_view(item: ClassificationView) -> ClassificationItemOut:
    return ClassificationItemOut(
        classification_id=item.classification_id,
        record_id=item.record_id,
        sequence_in_subject=item.sequence_in_subject,
        outcome=item.outcome,
        reason_category=item.reason_category,
        reason_text=item.reason_text,
        is_reclassification=item.is_reclassification,
        reinforced_reason=item.reinforced_reason,
        previous_outcome=item.previous_outcome,
        effect_on_loop=item.effect_on_loop,
        classified_at=format_timestamp(item.classified_at),
        classified_by=ClassifiedByOut(
            user_id=item.classified_by.user_id,
            display_name=item.classified_by.display_name,
            role_in_use=item.classified_by.role_in_use,
        ),
    )


def _record_view(record: RecordView) -> RecordOut:
    return RecordOut(
        record_id=record.record_id,
        record_type=record.record_type,
        chain_sequence=record.chain_sequence,
        content_hash=record.content_hash,
        segment_index=record.segment_index,
        received_at=format_timestamp(record.received_at),
        occurred_at=_stamp(record.occurred_at),
        signals=tuple(
            SignalOut(
                signal_id=signal.signal_id,
                role=signal.role,
                value=signal.value,
                valid=signal.valid,
                read_at=_stamp(signal.read_at),
            )
            for signal in record.signals
        ),
        clips=tuple(
            ClipOut(
                evidence_id=clip.evidence_id,
                clip_id=clip.clip_id,
                camera_id=clip.camera_id,
                segment=clip.segment,
                media_kind=clip.media_kind,
                content_type=clip.content_type,
                size_bytes=clip.size_bytes,
                sha256=clip.sha256,
                duration_ms=clip.duration_ms,
                starts_at=_stamp(clip.starts_at),
                ends_at=_stamp(clip.ends_at),
                read_url=clip.read_url,
            )
            for clip in record.clips
        ),
    )


def detail_view(detail: FindingDetail) -> FindingDetailOut:
    standard = detail.standard
    episode = detail.episode
    coverage = detail.coverage
    return FindingDetailOut(
        finding_state_id=detail.finding_state_id,
        subject_kind=detail.subject_kind,
        subject_id=detail.subject_id,
        plant_id=detail.plant_id,
        zone_id=detail.zone_id,
        family=detail.family,
        tier=detail.tier,
        lifecycle_state=detail.lifecycle_state,
        classification_outcome=detail.classification_outcome,
        classification_count=detail.classification_count,
        age_days=detail.age_days,
        observability_state_at_event=detail.observability_state_at_event,
        occurred_at=format_timestamp(detail.occurred_at),
        due_at=_stamp(detail.due_at),
        overdue_days=detail.overdue_days,
        time_aggregated=detail.time_aggregated,
        aggregation_window_minutes=detail.aggregation_window_minutes,
        evidence_flagged=detail.evidence_flagged,
        standard=StandardCitationOut(
            standard_id=standard.standard_id,
            version=standard.version,
            title_es=standard.title_es,
            declared_text=standard.declared_text,
            declared_text_sha256=standard.declared_text_sha256,
            effective_from=_stamp(standard.effective_from),
            superseded_since=_stamp(standard.superseded_since),
            superseded_in_catalog_version=standard.superseded_in_catalog_version,
        ),
        gate_at_event=GateAtEventOut(
            status=detail.gate.status,
            effective_from=format_timestamp(detail.gate.effective_from),
            effective_until=_stamp(detail.gate.effective_until),
        ),
        coverage_at_event=(
            None
            if coverage is None
            else CoverageAtEventOut(
                state=coverage.state, causes=coverage.causes, layer=coverage.layer
            )
        ),
        nodes=(
            None
            if detail.nodes is None
            else tuple(
                NodeOut(
                    node_id=node.node_id,
                    code=node.code,
                    communication_state=node.communication_state,
                    since=format_timestamp(node.since),
                    last_heartbeat_at=_stamp(node.last_heartbeat_at),
                    cameras=tuple(
                        NodeCameraOut(
                            camera_id=camera.camera_id,
                            code=camera.code,
                            connected=camera.connected,
                            observability_state=camera.observability_state,
                        )
                        for camera in node.cameras
                    ),
                )
                for node in detail.nodes
            )
        ),
        episode=(
            None
            if episode is None
            else EpisodeOut(
                episode_id=episode.episode_id,
                segment_record_ids=episode.segment_record_ids,
                segment_count=episode.segment_count,
                expected_segment_count=episode.expected_segment_count,
                missing_segment_indexes=episode.missing_segment_indexes,
                started_at=format_timestamp(episode.started_at),
                ended_at=format_timestamp(episode.ended_at),
                total_episode_duration_ms=episode.total_episode_duration_ms,
                total_condition_duration_ms=episode.total_condition_duration_ms,
                continues_open=episode.continues_open,
            )
        ),
        records=tuple(_record_view(record) for record in detail.records),
        classifications=tuple(_classification_view(c) for c in detail.classifications),
        actions=tuple(
            ActionOut(
                action_id=action.action_id,
                record_id=action.record_id,
                action_type=action.action_type,
                action_description=action.action_description,
                expected_outcome=action.expected_outcome,
                state=action.state,
                assigned_at=format_timestamp(action.assigned_at),
                due_at=format_timestamp(action.due_at),
                counts_as_overdue=action.counts_as_overdue,
                root_cause_analysis_id=action.root_cause_analysis_id,
            )
            for action in detail.actions
        ),
        analyses=tuple(
            AnalysisOut(
                analysis_id=analysis.analysis_id,
                record_id=analysis.record_id,
                state=analysis.state,
                root_cause_category=analysis.root_cause_category,
                root_cause_statement=analysis.root_cause_statement,
                conclusion=analysis.conclusion,
                signed_at=_stamp(analysis.signed_at),
                created_at=format_timestamp(analysis.created_at),
            )
            for analysis in detail.analyses
        ),
        notices=tuple(
            DetailNoticeOut(code=notice.code, since=_stamp(notice.since), cause=notice.cause)
            for notice in detail.notices
        ),
    )


def history_view(history: ClassificationHistory) -> ClassificationHistoryOut:
    return ClassificationHistoryOut(
        finding_state_id=history.finding_state_id,
        time_aggregated=history.time_aggregated,
        aggregation_window_minutes=history.aggregation_window_minutes,
        classifications=tuple(_classification_view(c) for c in history.classifications),
        review_resolutions=tuple(
            ReviewResolutionItemOut(
                resolution_id=item.resolution_id,
                record_id=item.record_id,
                outcome=item.outcome,
                reason_category=item.reason_category,
                reason_text=item.reason_text,
                resolved_at=format_timestamp(item.resolved_at),
                resolved_by=ClassifiedByOut(
                    user_id=item.resolved_by.user_id,
                    display_name=item.resolved_by.display_name,
                    role_in_use=item.resolved_by.role_in_use,
                ),
            )
            for item in history.review_resolutions
        ),
    )


def _states(values: list[str] | None) -> tuple[FindingLifecycleState, ...] | None:
    if not values:
        return None
    active = {state.value: state for state in INBOX_STATES}
    if any(value not in active for value in values):
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(active[value] for value in values))


def _zones(values: list[uuid.UUID] | None) -> tuple[uuid.UUID, ...] | None:
    if not values:
        return None
    if len(values) > MAX_ZONES:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(values))


def findings_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        PATH,
        dependencies=[
            requires(_READ),
            Depends(
                exact_query(
                    "family",
                    "tier",
                    "from",
                    "to",
                    "order",
                    "cursor",
                    "limit",
                    repeatable=("zone_id", "lifecycle_state"),
                )
            ),
        ],
        summary="Bandeja del lazo por zona, desde la proyección, por páginas de hasta 200",
    )
    async def list_findings(
        request: Request,
        response: Response,
        services: Services,
        zone_id: Annotated[list[uuid.UUID] | None, Query()] = None,
        family: PredicateFamily | None = None,
        tier: EvidenceTier | None = None,
        lifecycle_state: Annotated[list[str] | None, Query()] = None,
        from_: Annotated[str | None, Query(alias="from", max_length=_TIMESTAMP_CHARS)] = None,
        to: Annotated[str | None, Query(max_length=_TIMESTAMP_CHARS)] = None,
        order: InboxOrder = InboxOrder.NEWEST_FIRST,
        cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_INBOX_PAGE)] = MAX_INBOX_PAGE,
    ) -> InboxPageOut:
        no_store(response)
        filters = InboxFilters(
            zone_ids=_zones(zone_id),
            family=None if family is None else family.value,
            tier=None if tier is None else tier.value,
            lifecycle_states=_states(lifecycle_state),
            period_from=None if from_ is None else parse_instant(from_),
            period_to=None if to is None else parse_instant(to),
        )
        try:
            page = await installed(services.inbox).page(
                request_context(request),
                filters=filters,
                after=None if cursor is None else decode_inbox_cursor(cursor),
                limit=limit,
                newest_first=order is InboxOrder.NEWEST_FIRST,
            )
        except InboxRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return page_view(page)

    @router.get(
        DETAIL_PATH,
        dependencies=[
            requires(_READ, detail_codes=(_GATE_UNAVAILABLE.value,)),
            Depends(exact_query()),
        ],
        summary="Detalle auditado de un hallazgo con la cita del estándar y sus degradaciones",
    )
    async def get_finding(
        finding_state_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> FindingDetailOut:
        no_store(response)
        try:
            detail = await installed(services.detail).detail(
                request_context(request), finding_state_id
            )
        except FindingGateUnavailable:
            # Fallo cerrado (BR-LAZ-12): solo el error, ningún campo del hallazgo.
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[_GATE_UNAVAILABLE]
            raise ApiError(code, detail_code=_GATE_UNAVAILABLE.value) from None
        return detail_view(detail)

    @router.get(
        CLASSIFICATIONS_PATH,
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Historial completo de clasificaciones de un hallazgo, en orden",
    )
    async def list_classifications(
        finding_state_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> ClassificationHistoryOut:
        no_store(response)
        history = await installed(services.detail).classifications(
            request_context(request), finding_state_id
        )
        return history_view(history)

    return router
