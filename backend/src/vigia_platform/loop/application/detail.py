"""``loop.detail`` (LC-LAZ-06; C-PLA-22; SUP-U05-02 ``ver_detalle_hallazgo`` y SUP-U05-04
``consultar_historial_clasificaciones``): el detalle auditado de un hallazgo y su historial.

``FindingDetailService.detail(context, finding_state_id)``, en este orden:

1. **Alcance** (BR-LAZ-01): la fila de ``loop.finding_state`` de la organización y
   ``findings.read`` sobre **su zona**. Inexistente, de otra organización, de otra zona o planta
   fuera del alcance: ``ResourceNotFound`` (``not_found``, nunca ``forbidden``).
2. **Compuerta de uso del instante** por ``GateQueryPort.state_at`` (llamada 1 a U-03). Sin
   respuesta, **fallo cerrado**: ``FindingGateUnavailable`` (503 ``gate_state_unavailable``) y el
   hallazgo **no se muestra**, ni se lee el expediente (BR-LAZ-12, NFR-LAZ-29, PAT-LAZ-SEG-09). Si
   en el instante del hecho la compuerta no estaba ``approved``, el hallazgo no existe para nadie
   (``ResourceNotFound``), como en la bandeja.
3. **Proyección** (una transacción): el episodio con sus tramos faltantes declarados, las
   clasificaciones del sujeto por ``sequence_in_subject``, sus acciones y sus análisis.
4. **Expediente** por ``LectorExpediente.get`` de cada tramo presente del episodio (o del registro
   de anclaje de una detección): cada lectura deja su entrada ``ledger_detail_read`` con el
   identificador leído (BR-LAZ-09). De cada registro salen sus señales y sus clips (metadatos
   verificados, nunca el contenido).
5. **Lo que degrada declarando** (PAT-LAZ-RES-04; nunca oculta el hallazgo):
   ``CatalogQueryPort.standard_version`` (llamada 2) da el título, el texto y su ``sha256``; sin
   él, la cita ``(standard_id, version)`` y el aviso ``standard_text_unavailable``. Si la versión
   citada tiene ``effective_until``, el aviso ``later_standard_version`` con esa fecha: la versión
   dejó de regir entonces (sustituida o retirada; ``effective_until`` no lo distingue) y el
   hallazgo **conserva** la versión con la que se evaluó (BR-LAZ-152; nunca se reevalúa).
   ``FleetQueryPort.nodes_by_zone`` (llamada 3) da el estado de los nodos; sin él,
   ``node_state_unavailable``. ``CoveragePort.estado_en`` (U-02) da la cobertura del instante;
   sin ella, ``coverage_unavailable`` y la zona **nunca** se presenta como observada.
   ``EvidencePort.url_lectura`` da la URL de cada clip (≤ 5 min, auditada, BR-LAZ-08); un clip
   ilegible o sin almacén deja su referencia y ``evidence_url_unavailable``, y los demás clips se
   siguen pidiendo (solo un almacén caído corta el resto).

**A lo sumo tres llamadas a U-03** por detalle (NFR-LAZ-03): ``state_at``, ``standard_version``
y ``nodes_by_zone``, una vez cada una y nunca dentro de un bucle. La ocupación unipersonal, el
acuerdo revocado (con su fecha) y el acta no vigente salen de las columnas enriquecidas de la
proyección, como en la bandeja (cero llamadas).

``classifications(context, finding_state_id)`` (SUP-U05-04) es el historial completo del sujeto
desde ``loop.classification`` (una fila por registro ``classification``, ninguna se edita) en
orden de ``sequence_in_subject``, que es el orden de la cadena porque cada clasificación se
escribe bajo la exclusión del sujeto. Mismo alcance que el detalle; sin llamadas a U-03 (el
hallazgo sin compuerta de uso aprobada en el instante del hecho tampoco se muestra aquí, por la
columna de la proyección, como en la bandeja).

**Agregación temporal** (BR-LAZ-06 y 07, PR-LAZ-02): en una zona unipersonal **toda** marca del
detalle y del historial es el inicio de su ventana (``loop.domain.time_aggregation``); el
expediente conserva la marca exacta y su ``content_hash`` no cambia. ``finding_detail`` y
``classification_history`` son la composición **pura** de lo leído: la propiedad PR-LAZ-02 las
recorre sin base. Ningún dato de persona observada (BR-LAZ-02 y 03): el autor de una
clasificación es el usuario de la plataforma que decidió (P8).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import text
from vigia_contracts.models.enumerations import GateStatus

from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.catalog.domain.ports import CatalogQueryPort, GateQueryPort
from vigia_platform.catalog.domain.standard import DeclaredStandardVersion
from vigia_platform.fleet.ports import FleetQueryPort, ZoneNode
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.coverage import CoveragePort
from vigia_platform.ledger.application.evidence_read import (
    EvidenceNotFound,
    EvidencePort,
    EvidenceUnreadable,
)
from vigia_platform.ledger.application.reader import LedgerRecordView
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.domain.coverage import CoverageStatus
from vigia_platform.loop.application.classification import SUBJECT_RESOURCE_KIND
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingDetailNotice, FindingLifecycleState, SubjectKind
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.api.errors import ExternalDependencyDown
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "MAX_DETAIL_RECORDS",
    "ActionView",
    "AnalysisView",
    "ClassificationHistory",
    "ClassificationView",
    "ClipView",
    "CoverageAtEvent",
    "DetailInputs",
    "DetailNotice",
    "EpisodeView",
    "FindingDetail",
    "FindingDetailService",
    "FindingGateUnavailable",
    "GateAtEvent",
    "NodeView",
    "RecordReader",
    "RecordView",
    "SignalView",
    "StandardCitation",
    "classification_history",
    "finding_detail",
    "standard_text_sha256",
]

MAX_DETAIL_RECORDS: Final = 64
"""Tramos del expediente que el detalle lee como mucho ``[objetivo propio]``; un episodio con
más lo **declara** (``segment_records_limited``) en vez de alargar la lectura sin tope, y nunca
oculta los demás: sus identificadores siguen en ``episode.segment_record_ids``."""

_READ: Final = PermissionKey.FINDINGS_READ
_log = get_logger("loop.detail")

_SUBJECT: Final = text(
    "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind, subject_id,"
    " anchor_record_id, family, tier, standard_id, standard_version, lifecycle_state,"
    " current_classification_outcome, classification_count, due_at, overdue_days, age_days,"
    " observability_state_at_event, zone_gate_status_at_event, zone_agreement_revoked,"
    " zone_agreement_revoked_at, single_occupancy_zone, aggregation_window_minutes,"
    " walk_test_regression_pending, walk_test_regression_cause, occurred_at_exact,"
    " evidence_flagged, missing_segments_declared"
    " FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
)
_EPISODE: Final = text(
    "SELECT episode_id, segment_record_ids, segment_count, expected_segment_count,"
    " missing_segment_indexes, started_at, ended_at, total_episode_duration_ms,"
    " total_condition_duration_ms, continues_open"
    " FROM loop.episode WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = :zone_id AND episode_id = :subject_id"
)
_CLASSIFICATIONS: Final = text(
    "SELECT classification_id, record_id, sequence_in_subject, outcome, reason_category,"
    " reason_text, is_reclassification, reinforced_reason, previous_outcome, effect_on_loop,"
    " classified_by, classified_at"
    " FROM loop.classification WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = :zone_id AND subject_id = :subject_id"
    " ORDER BY sequence_in_subject, classification_id"
)
_ACTIONS: Final = text(
    "SELECT action_id, record_id, action_type, action_description, expected_outcome, state,"
    " assigned_at, due_at, counts_as_overdue, root_cause_analysis_id"
    " FROM loop.corrective_action WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND zone_id = :zone_id AND subject_id = :subject_id"
    " ORDER BY assigned_at, action_id"
)
_ANALYSES: Final = text(
    "SELECT analysis_id, record_id, state, root_cause_category, root_cause_statement, conclusion,"
    " signed_at, created_at"
    " FROM loop.root_cause_analysis WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND zone_id = :zone_id AND subject_id = :subject_id"
    " ORDER BY created_at, analysis_id"
)


class FindingGateUnavailable(ExternalDependencyDown):
    """``GateQueryPort`` no respondió: el hallazgo no se muestra (503 ``gate_state_unavailable``).

    Es ``ExternalDependencyDown``: aun sin la traducción de la ruta, la respuesta es un 503 y
    nunca el hallazgo."""

    detail_code: Final = LoopDetailCode.GATE_STATE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("gate_query_port")


class RecordReader(Protocol):
    """Lo que el detalle usa de ``LectorExpediente``: ``get``, auditado."""

    async def get(
        self,
        context: ScopeContext,
        record_id: uuid.UUID,
        *,
        record_types: tuple[str, ...] | None = None,
    ) -> LedgerRecordView | None: ...


# --- valores del detalle ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetailNotice:
    code: FindingDetailNotice
    since: datetime | None = None
    """Desde cuándo (acuerdo revocado, versión posterior), agregada en zona unipersonal."""
    cause: str | None = None
    """La causa que U-03 declara (acta no vigente)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StandardCitation:
    """La versión con la que se evaluó, para siempre (H-16); el texto puede faltar (degradado)."""

    standard_id: uuid.UUID
    version: int
    title_es: str | None
    declared_text: str | None
    declared_text_sha256: str | None
    effective_from: datetime | None
    superseded_since: datetime | None
    """Desde cuándo rige una versión posterior del catálogo (BR-LAZ-152)."""
    superseded_in_catalog_version: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class GateAtEvent:
    """La compuerta de uso en el instante del hecho (siempre ``approved``: si no, no se muestra)."""

    status: str
    effective_from: datetime
    effective_until: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageAtEvent:
    state: str
    causes: tuple[str, ...]
    layer: str


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeCameraView:
    camera_id: uuid.UUID
    code: str | None
    connected: bool
    observability_state: str


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeView:
    node_id: uuid.UUID
    code: str
    communication_state: str
    since: datetime
    last_heartbeat_at: datetime | None
    cameras: tuple[NodeCameraView, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalView:
    signal_id: uuid.UUID | None
    role: str
    value: str
    valid: bool
    read_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClipView:
    """Un clip del expediente: la referencia siempre; la URL solo si el almacén la concede."""

    evidence_id: uuid.UUID
    clip_id: uuid.UUID
    camera_id: uuid.UUID
    segment: str
    media_kind: str
    content_type: str
    size_bytes: int
    sha256: str
    duration_ms: int | None
    starts_at: datetime | None
    ends_at: datetime | None
    read_url: str | None
    """URL prefirmada de solo lectura, vigente a lo sumo 5 minutos (BR-NUC-66)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordView:
    """Un registro del expediente leído por ``LectorExpediente`` (auditado)."""

    record_id: uuid.UUID
    record_type: str
    chain_sequence: int
    content_hash: str
    segment_index: int | None
    received_at: datetime
    occurred_at: datetime | None
    signals: tuple[SignalView, ...]
    clips: tuple[ClipView, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class EpisodeView:
    episode_id: uuid.UUID
    segment_record_ids: tuple[uuid.UUID, ...]
    segment_count: int
    expected_segment_count: int
    missing_segment_indexes: tuple[int, ...]
    started_at: datetime
    ended_at: datetime
    total_episode_duration_ms: int
    total_condition_duration_ms: int
    continues_open: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ClassifiedBy:
    """El usuario de la plataforma que clasificó (P8), nunca una persona observada."""

    user_id: uuid.UUID
    display_name: str
    role_in_use: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ClassificationView:
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
    classified_at: datetime
    classified_by: ClassifiedBy


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionView:
    action_id: uuid.UUID
    record_id: uuid.UUID
    action_type: str
    action_description: str
    expected_outcome: str | None
    state: str
    assigned_at: datetime
    due_at: datetime
    counts_as_overdue: bool
    root_cause_analysis_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisView:
    analysis_id: uuid.UUID
    record_id: uuid.UUID | None
    state: str
    root_cause_category: str | None
    root_cause_statement: str | None
    conclusion: str | None
    signed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingDetail:
    """SUP-U05-02: el hallazgo con todas sus marcas presentadas y sus secciones degradadas."""

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
    occurred_at: datetime
    due_at: datetime | None
    overdue_days: int
    time_aggregated: bool
    aggregation_window_minutes: int | None
    evidence_flagged: bool
    standard: StandardCitation
    gate: GateAtEvent
    coverage: CoverageAtEvent | None
    nodes: tuple[NodeView, ...] | None
    episode: EpisodeView | None
    records: tuple[RecordView, ...]
    classifications: tuple[ClassificationView, ...]
    actions: tuple[ActionView, ...]
    analyses: tuple[AnalysisView, ...]
    notices: tuple[DetailNotice, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ClassificationHistory:
    """SUP-U05-04: todas las clasificaciones del sujeto, en orden."""

    finding_state_id: uuid.UUID
    time_aggregated: bool
    aggregation_window_minutes: int | None
    classifications: tuple[ClassificationView, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DetailInputs:
    """Lo leído para un detalle, con las marcas **exactas**; ``None`` = el puerto no respondió."""

    subject: Mapping[str, Any]
    """La fila de ``loop.finding_state``."""
    gate: GateInterval
    standard: DeclaredStandardVersion | None
    nodes: Sequence[ZoneNode] | None
    coverage: CoverageStatus | None
    episode: Mapping[str, Any] | None
    records: Sequence[LedgerRecordView]
    records_limited: bool
    """El episodio tiene más de ``MAX_DETAIL_RECORDS`` tramos: se leyeron los primeros."""
    read_urls: Mapping[uuid.UUID, str]
    """URL concedida por evidencia; una evidencia que falta aquí no tuvo URL."""
    evidence_urls_unavailable: bool
    classifications: Sequence[Mapping[str, Any]]
    actions: Sequence[Mapping[str, Any]]
    analyses: Sequence[Mapping[str, Any]]


# --- composición pura ------------------------------------------------------------------------


def standard_text_sha256(declared_text: str) -> str:
    """El ``sha256`` del texto declarado en UTF-8 (el mismo que cita la exportación)."""
    return hashlib.sha256(declared_text.encode("utf-8")).hexdigest()


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _optional_uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _instant(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria")
    return moment


def _optional_instant(value: Any) -> datetime | None:
    return None if value is None else _instant(value)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


class _Presenter:
    """La marca que se devuelve: agregada en zona unipersonal, exacta si no (BR-LAZ-06)."""

    def __init__(self, subject: Mapping[str, Any]) -> None:
        self.single = bool(subject["single_occupancy_zone"])
        self.window = int(subject["aggregation_window_minutes"])

    def __call__(self, moment: Any) -> datetime:
        return presented(_instant(moment), single_occupancy=self.single, window_minutes=self.window)

    def optional(self, moment: Any) -> datetime | None:
        return None if moment is None else self(moment)

    @property
    def window_minutes(self) -> int | None:
        return self.window if self.single else None


def _classification(row: Mapping[str, Any], shown: _Presenter) -> ClassificationView:
    author = _json(row["classified_by"])
    return ClassificationView(
        classification_id=_uuid(row["classification_id"]),
        record_id=_uuid(row["record_id"]),
        sequence_in_subject=int(row["sequence_in_subject"]),
        outcome=str(row["outcome"]),
        reason_category=str(row["reason_category"]),
        reason_text=str(row["reason_text"]),
        is_reclassification=bool(row["is_reclassification"]),
        reinforced_reason=bool(row["reinforced_reason"]),
        previous_outcome=row["previous_outcome"],
        effect_on_loop=str(row["effect_on_loop"]),
        classified_at=shown(row["classified_at"]),
        classified_by=ClassifiedBy(
            user_id=_uuid(author["user_id"]),
            display_name=str(author["display_name"]),
            role_in_use=str(author["role_in_use"]),
        ),
    )


def classification_history(
    subject: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> ClassificationHistory:
    """El historial con sus marcas presentadas; ``rows`` ya viene en orden de secuencia."""
    shown = _Presenter(subject)
    return ClassificationHistory(
        finding_state_id=_uuid(subject["finding_state_id"]),
        time_aggregated=shown.single,
        aggregation_window_minutes=shown.window_minutes,
        classifications=tuple(_classification(row, shown) for row in rows),
    )


def _standard(
    subject: Mapping[str, Any], declared: DeclaredStandardVersion | None, shown: _Presenter
) -> StandardCitation:
    standard_id = _uuid(subject["standard_id"])
    version = int(subject["standard_version"])
    if declared is None:
        return StandardCitation(
            standard_id=standard_id,
            version=version,
            title_es=None,
            declared_text=None,
            declared_text_sha256=None,
            effective_from=None,
            superseded_since=None,
            superseded_in_catalog_version=None,
        )
    return StandardCitation(
        standard_id=standard_id,
        version=version,
        title_es=declared.title_es,
        declared_text=declared.declared_text,
        declared_text_sha256=standard_text_sha256(declared.declared_text),
        effective_from=shown(declared.effective_from),
        superseded_since=shown.optional(declared.effective_until),
        superseded_in_catalog_version=declared.retired_in_catalog_version,
    )


def _node(node: ZoneNode, shown: _Presenter) -> NodeView:
    return NodeView(
        node_id=node.node_id,
        code=node.code,
        communication_state=node.communication_state.value,
        since=shown(node.since),
        last_heartbeat_at=shown.optional(node.last_heartbeat_at),
        cameras=tuple(
            NodeCameraView(
                camera_id=camera.camera_id,
                code=camera.code,
                connected=camera.connected,
                observability_state=camera.observability_state,
            )
            for camera in node.cameras
        ),
    )


def _signals(document: Mapping[str, Any], shown: _Presenter) -> tuple[SignalView, ...]:
    signals: Sequence[Mapping[str, Any]] = document.get("signals") or ()
    return tuple(
        SignalView(
            signal_id=_optional_uuid(signal.get("signal_id")),
            role=str(signal["role"]),
            value=str(signal["value"]),
            valid=bool(signal["valid"]),
            read_at=shown.optional(_optional_instant(signal.get("read_at"))),
        )
        for signal in signals
    )


def _segment_index(document: Mapping[str, Any]) -> int | None:
    episode = document.get("episode")
    if isinstance(episode, Mapping) and isinstance(episode.get("segment_index"), int):
        return int(episode["segment_index"])
    return None


def _record(
    record: LedgerRecordView, read_urls: Mapping[uuid.UUID, str], shown: _Presenter
) -> RecordView:
    document = record.document()
    document = document if isinstance(document, Mapping) else {}
    return RecordView(
        record_id=record.record_id,
        record_type=record.record_type,
        chain_sequence=record.chain_sequence,
        content_hash=record.content_hash,
        segment_index=_segment_index(document),
        received_at=shown(record.received_at),
        occurred_at=shown.optional(record.occurred_at),
        signals=_signals(document, shown),
        clips=tuple(
            ClipView(
                evidence_id=evidence.evidence_id,
                clip_id=evidence.clip_id,
                camera_id=evidence.camera_id,
                segment=evidence.segment,
                media_kind=evidence.media_kind,
                content_type=evidence.content_type,
                size_bytes=evidence.size_bytes,
                sha256=evidence.sha256,
                duration_ms=evidence.duration_ms,
                starts_at=shown.optional(evidence.starts_at),
                ends_at=shown.optional(evidence.ends_at),
                read_url=read_urls.get(evidence.evidence_id),
            )
            for evidence in record.evidences
        ),
    )


def _episode(row: Mapping[str, Any], shown: _Presenter) -> EpisodeView:
    return EpisodeView(
        episode_id=_uuid(row["episode_id"]),
        segment_record_ids=tuple(_uuid(i) for i in row["segment_record_ids"]),
        segment_count=int(row["segment_count"]),
        expected_segment_count=int(row["expected_segment_count"]),
        missing_segment_indexes=tuple(int(i) for i in row["missing_segment_indexes"]),
        started_at=shown(row["started_at"]),
        ended_at=shown(row["ended_at"]),
        total_episode_duration_ms=int(row["total_episode_duration_ms"]),
        total_condition_duration_ms=int(row["total_condition_duration_ms"]),
        continues_open=bool(row["continues_open"]),
    )


def _action(row: Mapping[str, Any], shown: _Presenter) -> ActionView:
    return ActionView(
        action_id=_uuid(row["action_id"]),
        record_id=_uuid(row["record_id"]),
        action_type=str(row["action_type"]),
        action_description=str(row["action_description"]),
        expected_outcome=row["expected_outcome"],
        state=str(row["state"]),
        assigned_at=shown(row["assigned_at"]),
        due_at=shown(row["due_at"]),
        counts_as_overdue=bool(row["counts_as_overdue"]),
        root_cause_analysis_id=_optional_uuid(row["root_cause_analysis_id"]),
    )


def _analysis(row: Mapping[str, Any], shown: _Presenter) -> AnalysisView:
    return AnalysisView(
        analysis_id=_uuid(row["analysis_id"]),
        record_id=_optional_uuid(row["record_id"]),
        state=str(row["state"]),
        root_cause_category=row["root_cause_category"],
        root_cause_statement=row["root_cause_statement"],
        conclusion=row["conclusion"],
        signed_at=shown.optional(_optional_instant(row["signed_at"])),
        created_at=shown(row["created_at"]),
    )


def _notices(inputs: DetailInputs, shown: _Presenter) -> tuple[DetailNotice, ...]:
    subject = inputs.subject
    notices: list[DetailNotice] = []
    if subject["zone_agreement_revoked"]:
        notices.append(
            DetailNotice(
                FindingDetailNotice.AGREEMENT_REVOKED,
                since=shown.optional(_optional_instant(subject["zone_agreement_revoked_at"])),
            )
        )
    if subject["walk_test_regression_pending"]:
        notices.append(
            DetailNotice(
                FindingDetailNotice.WALK_TEST_REGRESSION_PENDING,
                cause=subject["walk_test_regression_cause"],
            )
        )
    if subject["evidence_flagged"]:
        notices.append(DetailNotice(FindingDetailNotice.EVIDENCE_FLAGGED))
    if subject["missing_segments_declared"]:
        notices.append(DetailNotice(FindingDetailNotice.MISSING_SEGMENTS_DECLARED))
    if inputs.standard is None:
        notices.append(DetailNotice(FindingDetailNotice.STANDARD_TEXT_UNAVAILABLE))
    elif inputs.standard.effective_until is not None:
        notices.append(
            DetailNotice(
                FindingDetailNotice.LATER_STANDARD_VERSION,
                since=shown(inputs.standard.effective_until),
            )
        )
    if inputs.coverage is None:
        notices.append(DetailNotice(FindingDetailNotice.COVERAGE_UNAVAILABLE))
    if inputs.nodes is None:
        notices.append(DetailNotice(FindingDetailNotice.NODE_STATE_UNAVAILABLE))
    if inputs.records_limited:
        notices.append(DetailNotice(FindingDetailNotice.SEGMENT_RECORDS_LIMITED))
    if inputs.evidence_urls_unavailable:
        notices.append(DetailNotice(FindingDetailNotice.EVIDENCE_URL_UNAVAILABLE))
    return tuple(notices)


def finding_detail(inputs: DetailInputs) -> FindingDetail:
    """El detalle con **todas** sus marcas presentadas; lo leído no cambia (PR-LAZ-02)."""
    subject = inputs.subject
    shown = _Presenter(subject)
    gate = inputs.gate
    coverage = inputs.coverage
    return FindingDetail(
        finding_state_id=_uuid(subject["finding_state_id"]),
        subject_kind=str(subject["subject_kind"]),
        subject_id=_uuid(subject["subject_id"]),
        plant_id=_uuid(subject["plant_id"]),
        zone_id=_uuid(subject["zone_id"]),
        family=str(subject["family"]),
        tier=str(subject["tier"]),
        lifecycle_state=FindingLifecycleState(subject["lifecycle_state"]),
        classification_outcome=subject["current_classification_outcome"],
        classification_count=int(subject["classification_count"]),
        age_days=int(subject["age_days"]),
        observability_state_at_event=str(subject["observability_state_at_event"]),
        occurred_at=shown(subject["occurred_at_exact"]),
        due_at=shown.optional(_optional_instant(subject["due_at"])),
        overdue_days=int(subject["overdue_days"]),
        time_aggregated=shown.single,
        aggregation_window_minutes=shown.window_minutes,
        evidence_flagged=bool(subject["evidence_flagged"]),
        standard=_standard(subject, inputs.standard, shown),
        gate=GateAtEvent(
            status=gate.status.value,
            effective_from=shown(gate.effective_from),
            effective_until=shown.optional(gate.effective_until),
        ),
        coverage=(
            None
            if coverage is None
            else CoverageAtEvent(
                state=coverage.state.value,
                causes=tuple(str(cause) for cause in coverage.causes),
                layer=coverage.layer.value,
            )
        ),
        nodes=None if inputs.nodes is None else tuple(_node(n, shown) for n in inputs.nodes),
        episode=None if inputs.episode is None else _episode(inputs.episode, shown),
        records=tuple(_record(r, inputs.read_urls, shown) for r in inputs.records),
        classifications=tuple(_classification(r, shown) for r in inputs.classifications),
        actions=tuple(_action(r, shown) for r in inputs.actions),
        analyses=tuple(_analysis(r, shown) for r in inputs.analyses),
        notices=_notices(inputs, shown),
    )


# --- el servicio -----------------------------------------------------------------------------


def _mappings(rows: Sequence[Any]) -> list[Mapping[str, Any]]:
    return [row._mapping for row in rows]


@repository
class FindingDetailService:
    """``ver_detalle_hallazgo`` (SUP-U05-02) y ``consultar_historial_clasificaciones`` (04)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: Authorizer,
        reader: RecordReader,
        evidence: EvidencePort,
        catalog: CatalogQueryPort,
        gates: GateQueryPort,
        fleet: FleetQueryPort,
        coverage: CoveragePort,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._reader = reader
        self._evidence = evidence
        self._catalog = catalog
        self._gates = gates
        self._fleet = fleet
        self._coverage = coverage

    def __repr__(self) -> str:
        return "FindingDetailService()"

    async def detail(self, context: ScopeContext, finding_state_id: uuid.UUID) -> FindingDetail:
        """El detalle auditado; ``FindingGateUnavailable`` si la compuerta no se puede leer."""
        authorized, subject = await self._authorized(context, finding_state_id)
        zone_id = _uuid(subject["zone_id"])
        occurred_at = _instant(subject["occurred_at_exact"])
        gate = await self._gate(authorized, zone_id, occurred_at)
        episode, classifications, actions, analyses = await self._projection(authorized, subject)
        records, limited = await self._records(authorized, subject, episode)
        read_urls, urls_unavailable = await self._read_urls(authorized, records)
        return finding_detail(
            DetailInputs(
                subject=subject,
                gate=gate,
                standard=await self._standard(authorized, subject),
                nodes=await self._nodes(authorized, zone_id),
                coverage=await self._coverage_at(authorized, zone_id, occurred_at),
                episode=episode,
                records=records,
                records_limited=limited,
                read_urls=read_urls,
                evidence_urls_unavailable=urls_unavailable,
                classifications=classifications,
                actions=actions,
                analyses=analyses,
            )
        )

    async def classifications(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> ClassificationHistory:
        """El historial completo de clasificaciones del sujeto, desde la proyección."""
        authorized, subject = await self._authorized(context, finding_state_id)
        if subject["zone_gate_status_at_event"] != GateStatus.APPROVED.value:
            raise ResourceNotFound()  # BR-LAZ-12, como la bandeja: sin acuerdo no hay dato
        rows = await self._database.read(authorized, _CLASSIFICATIONS, _place(subject))
        return classification_history(subject, _mappings(rows))

    # --- pasos -----------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, Mapping[str, Any]]:
        """La fila de la organización y ``findings.read`` sobre **su zona** (BR-LAZ-01)."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _SUBJECT,
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        subject: Mapping[str, Any] = dict(rows[0]._mapping)
        resource = Resource(
            _uuid(subject["organization_id"]),
            SUBJECT_RESOURCE_KIND,
            _uuid(subject["finding_state_id"]),
            plant_id=_uuid(subject["plant_id"]),
            zone_id=_uuid(subject["zone_id"]),
        )
        authorized = await self._authorizer.authorize(context, _READ, resource)
        return authorized, subject

    async def _gate(self, context: ScopeContext, zone_id: uuid.UUID, at: datetime) -> GateInterval:
        """Llamada 1 a U-03. Sin respuesta, fallo cerrado; sin acuerdo en el instante, no existe."""
        try:
            gate = await self._gates.state_at(context, zone_id, GateKind.USAGE, at)
        except Exception:
            _log.exception("compuerta no legible en el detalle")
            raise FindingGateUnavailable() from None
        if gate is not None and (gate.zone_id != zone_id or gate.gate is not GateKind.USAGE):
            _log.warning("la compuerta devuelta no es la de uso de la zona del hallazgo")
            raise FindingGateUnavailable()
        if gate is None or gate.status is not GateStatus.APPROVED:
            raise ResourceNotFound()
        return gate

    async def _projection(
        self, context: ScopeContext, subject: Mapping[str, Any]
    ) -> tuple[
        Mapping[str, Any] | None,
        list[Mapping[str, Any]],
        list[Mapping[str, Any]],
        list[Mapping[str, Any]],
    ]:
        place = _place(subject)
        async with self._database.transaction(context) as transaction:
            episode = None
            if subject["subject_kind"] == SubjectKind.FINDING.value:
                found = (await transaction.execute(_EPISODE, place)).first()
                episode = None if found is None else found._mapping
            classifications = (await transaction.execute(_CLASSIFICATIONS, place)).all()
            actions = (await transaction.execute(_ACTIONS, place)).all()
            analyses = (await transaction.execute(_ANALYSES, place)).all()
        return (
            episode,
            _mappings(classifications),
            _mappings(actions),
            _mappings(analyses),
        )

    async def _records(
        self,
        context: ScopeContext,
        subject: Mapping[str, Any],
        episode: Mapping[str, Any] | None,
    ) -> tuple[tuple[LedgerRecordView, ...], bool]:
        """Cada tramo presente por ``LectorExpediente.get``: una entrada de auditoría por tramo."""
        identifiers: list[uuid.UUID] = (
            [_uuid(i) for i in episode["segment_record_ids"]]
            if episode is not None and episode["segment_record_ids"]
            else [_uuid(subject["anchor_record_id"])]
        )
        records: list[LedgerRecordView] = []
        for record_id in identifiers[:MAX_DETAIL_RECORDS]:
            record = await self._reader.get(context, record_id)
            if record is None:
                # El expediente manda: un tramo que el alcance no ve no se inventa.
                raise ResourceNotFound()
            records.append(record)
        return tuple(records), len(identifiers) > MAX_DETAIL_RECORDS

    async def _read_urls(
        self, context: ScopeContext, records: Sequence[LedgerRecordView]
    ) -> tuple[dict[uuid.UUID, str], bool]:
        """La URL de cada clip; sin almacén, la referencia y el aviso (nunca oculta el hallazgo)."""
        urls: dict[uuid.UUID, str] = {}
        tried: set[uuid.UUID] = set()
        unavailable = False
        for evidence_id in (e.evidence_id for r in records for e in r.evidences):
            if evidence_id in tried:
                continue
            tried.add(evidence_id)
            try:
                grant = await self._evidence.url_lectura(context, evidence_id)
            except (EvidenceNotFound, EvidenceUnreadable):
                # Solo este clip queda sin URL; los demás se siguen pidiendo (BR-LAZ-08).
                unavailable = True
                continue
            except Exception:
                # Almacén o base caídos: no se insiste con el resto de los clips.
                _log.exception("URL de evidencia no disponible")
                unavailable = True
                break
            urls[evidence_id] = grant.url
        return urls, unavailable

    async def _standard(
        self, context: ScopeContext, subject: Mapping[str, Any]
    ) -> DeclaredStandardVersion | None:
        """Llamada 2 a U-03: la versión citada, con su texto; si no, degradado."""
        zone_id = _uuid(subject["zone_id"])
        standard_id = _uuid(subject["standard_id"])
        version = int(subject["standard_version"])
        try:
            declared = await self._catalog.standard_version(context, zone_id, standard_id, version)
        except Exception:
            _log.exception("texto del estándar no disponible")
            return None
        if (
            declared.zone_id != zone_id
            or declared.standard_id != standard_id
            or declared.version != version
            or not declared.declared_text
        ):
            return None
        return declared

    async def _nodes(self, context: ScopeContext, zone_id: uuid.UUID) -> Sequence[ZoneNode] | None:
        """Llamada 3 a U-03: el estado de los nodos de la zona; si no, degradado."""
        try:
            return tuple(await self._fleet.nodes_by_zone(context, zone_id))
        except Exception:
            _log.exception("estado del nodo no disponible")
            return None

    async def _coverage_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> CoverageStatus | None:
        """``CoveragePort.estado_en`` (U-02); sin ella, la zona nunca se presenta como observada."""
        try:
            return await self._coverage.estado_en(context, zone_id, at)
        except Exception:
            _log.exception("cobertura del instante no disponible")
            return None


def _place(subject: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "organization_id": _uuid(subject["organization_id"]),
        "plant_id": _uuid(subject["plant_id"]),
        "zone_id": _uuid(subject["zone_id"]),
        "subject_id": _uuid(subject["subject_id"]),
    }
