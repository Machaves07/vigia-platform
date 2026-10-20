"""Generadores del detalle de un hallazgo y de su historial (TASK-342; PR-LAZ-02 ampliada).

``detail_inputs(zone)`` genera, para una zona de ``zones_with_privacy``, todo lo que el servicio
lee para un detalle (``loop.application.detail.DetailInputs``) con marcas en cualquier instante
(con microsegundos y zonas horarias distintas de UTC): la fila del sujeto (la de la bandeja, con
lo que el detalle añade), la compuerta del instante, el estándar con o sin versión posterior (o
``None``: el puerto no respondió), los nodos, la cobertura, el episodio, los registros del
expediente con sus señales y clips, y las clasificaciones, acciones y análisis de la proyección.

``marks(inputs)`` es el **oráculo**: la lista de todas las marcas reales que el detalle presenta,
una por cada vez que salen en la respuesta. Los textos libres generados no contienen dígitos, así
que ninguno puede confundirse con una marca de la respuesta.

Solo datos generados; ninguna marca sale de la hora real.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Final

from hypothesis import strategies as st
from vigia_contracts.models.enumerations import GateStatus, PredicateFamily

from tests.properties.laz.strategies.inbox import PrivacyZone, findings_over_time, instants
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.catalog.domain.standard import DeclaredBy, DeclaredStandardVersion
from vigia_platform.fleet.ports import ZoneNode, ZoneNodeCamera
from vigia_platform.ledger.application.reader import (
    EvidenceReference,
    LedgerRecordView,
    RecordActor,
)
from vigia_platform.ledger.domain.coverage import (
    ClockBasis,
    CommunicationState,
    CoverageLayer,
    CoverageState,
    CoverageStatus,
)
from vigia_platform.loop.application.detail import DetailInputs

__all__ = ["classification_rows", "detail_inputs", "history_marks", "marks"]

_TEXT: Final = st.text(alphabet="abcdefghijklmnñopqrstuvwxyz áéí", min_size=1, max_size=40)
"""Texto libre sin dígitos: nunca se parece a una marca de tiempo."""
_HASH: Final = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _later(start: datetime) -> st.SearchStrategy[datetime]:
    """Un instante posterior a ``start`` (con otra zona horaria a veces)."""
    return st.integers(1, 10**9).map(
        lambda ms: (start + timedelta(milliseconds=ms)).astimezone(start.tzinfo)
    )


@st.composite
def _subject(draw: st.DrawFn, zone: PrivacyZone) -> dict[str, Any]:
    (row,) = draw(findings_over_time(zone, min_size=1, max_size=1))
    return {
        **row,
        "subject_id": str(draw(st.uuids())),
        "anchor_record_id": str(draw(st.uuids())),
        "zone_gate_status_at_event": "approved",
    }


@st.composite
def _gate(draw: st.DrawFn, zone: PrivacyZone) -> GateInterval:
    start = draw(instants())
    return GateInterval(
        organization_id=draw(st.uuids()),
        plant_id=draw(st.uuids()),
        zone_id=zone.zone_id,
        gate=GateKind.USAGE,
        status=GateStatus.APPROVED,
        effective_from=start,
        effective_until=draw(st.none() | _later(start)),
        decided_by=draw(st.uuids()),
        reason_es=None,
        record_id=None,
        ledger_record_id=draw(st.uuids()),
    )


@st.composite
def _standard(draw: st.DrawFn, zone: PrivacyZone, subject: dict[str, Any]) -> Any:
    start = draw(instants())
    retired = draw(st.booleans())
    catalog_version = draw(st.integers(1, 1000))
    return DeclaredStandardVersion(
        organization_id=draw(st.uuids()),
        plant_id=uuid.UUID(subject["plant_id"]),
        zone_id=zone.zone_id,
        standard_id=uuid.UUID(subject["standard_id"]),
        version=int(subject["standard_version"]),
        family=draw(st.sampled_from(list(PredicateFamily))),
        title_es=draw(_TEXT),
        declared_text=draw(_TEXT),
        declared_by=DeclaredBy(draw(st.uuids()), "Firmante", "coordinator_sst"),
        effective_from=start,
        predicate={},
        catalog_version=catalog_version,
        reason_es="motivo",
        retired_in_catalog_version=catalog_version + 1 if retired else None,
        effective_until=draw(_later(start)) if retired else None,
    )


@st.composite
def _node(draw: st.DrawFn) -> ZoneNode:
    return ZoneNode(
        node_id=draw(st.uuids()),
        code="ND",
        communication_state=draw(st.sampled_from(list(CommunicationState))),
        since=draw(instants()),
        last_heartbeat_at=draw(st.none() | instants()),
        cameras=(ZoneNodeCamera(draw(st.uuids()), "CAM", draw(st.booleans()), 5.0, "observable"),),
    )


@st.composite
def _coverage(draw: st.DrawFn) -> CoverageStatus:
    return CoverageStatus(
        instant=draw(instants()),
        state=draw(st.sampled_from(list(CoverageState))),
        causes=(),
        layer=CoverageLayer.NODE_REPORT,
        clock_basis=ClockBasis.PLATFORM,
        source_record_ids=(),
    )


@st.composite
def _episode(draw: st.DrawFn, subject: dict[str, Any]) -> dict[str, Any]:
    started = draw(instants())
    return {
        "episode_id": subject["subject_id"],
        "segment_record_ids": [str(draw(st.uuids()))],
        "segment_count": 1,
        "expected_segment_count": draw(st.integers(1, 5)),
        "missing_segment_indexes": draw(st.lists(st.integers(1, 4), max_size=3, unique=True)),
        "started_at": _iso(started),
        "ended_at": _iso(draw(_later(started))),
        "total_episode_duration_ms": draw(st.integers(0, 10**7)),
        "total_condition_duration_ms": draw(st.integers(0, 10**7)),
        "continues_open": draw(st.booleans()),
    }


@st.composite
def _evidence(draw: st.DrawFn) -> EvidenceReference:
    starts = draw(st.none() | instants())
    return EvidenceReference(
        evidence_id=draw(st.uuids()),
        clip_id=draw(st.uuids()),
        camera_id=draw(st.uuids()),
        sha256=draw(_HASH),
        size_bytes=draw(st.integers(1, 10**8)),
        content_type="video/mp4",
        media_kind="video",
        duration_ms=draw(st.none() | st.integers(1, 10**6)),
        segment="full",
        starts_at=starts,
        ends_at=None if starts is None else draw(_later(starts)),
        verified_at=draw(instants()),
    )


@st.composite
def _record(draw: st.DrawFn, subject: dict[str, Any]) -> LedgerRecordView:
    signals = [
        {
            "signal_id": str(draw(st.uuids())),
            "role": "energy",
            "value": draw(st.sampled_from(("asserted", "not_asserted"))),
            "valid": draw(st.booleans()),
            "read_at": _iso(draw(instants())),
        }
        for _ in range(draw(st.integers(0, 3)))
    ]
    document = {"signals": signals, "episode": {"segment_index": draw(st.integers(0, 4))}}
    return LedgerRecordView(
        record_id=draw(st.uuids()),
        organization_id=draw(st.uuids()),
        plant_id=uuid.UUID(subject["plant_id"]),
        chain_sequence=draw(st.integers(1, 10**9)),
        record_type="finding",
        schema_version=1,
        actor=RecordActor("node", draw(st.uuids()), "Nodo", None, None, "u03"),
        scope_plant_id=uuid.UUID(subject["plant_id"]),
        scope_zone_id=uuid.UUID(subject["zone_id"]),
        scope_node_id=draw(st.uuids()),
        correlation_id=draw(st.uuids()),
        received_at=draw(instants()),
        occurred_at=draw(st.none() | instants()),
        source_key=None,
        content=json.dumps(document).encode(),
        content_hash=draw(_HASH),
        previous_hash=draw(_HASH),
        record_hash=draw(_HASH),
        evidences=tuple(draw(st.lists(_evidence(), max_size=3))),
    )


@st.composite
def _classification(draw: st.DrawFn, sequence: int) -> dict[str, Any]:
    return {
        "classification_id": str(draw(st.uuids())),
        "record_id": str(draw(st.uuids())),
        "sequence_in_subject": sequence,
        "outcome": "confirmed",
        "reason_category": "real_coexistence",
        "reason_text": draw(_TEXT),
        "is_reclassification": sequence > 1,
        "reinforced_reason": sequence > 1,
        "previous_outcome": "confirmed" if sequence > 1 else None,
        "effect_on_loop": "none",
        "classified_by": json.dumps(
            {"user_id": str(draw(st.uuids())), "display_name": "Firmante", "role_in_use": "x"}
        ),
        "classified_at": _iso(draw(instants())),
    }


def classification_rows() -> st.SearchStrategy[list[dict[str, Any]]]:
    """Filas de ``loop.classification`` de un sujeto, en orden de secuencia."""
    return st.integers(0, 6).flatmap(
        lambda count: st.tuples(*(_classification(n + 1) for n in range(count))).map(list)
    )


@st.composite
def _action(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "action_id": str(draw(st.uuids())),
        "record_id": str(draw(st.uuids())),
        "action_type": "engineering",
        "action_description": draw(_TEXT),
        "expected_outcome": draw(st.none() | _TEXT),
        "state": "open",
        "assigned_at": _iso(draw(instants())),
        "due_at": _iso(draw(instants())),
        "counts_as_overdue": draw(st.booleans()),
        "root_cause_analysis_id": None,
    }


@st.composite
def _analysis(draw: st.DrawFn) -> dict[str, Any]:
    signed = draw(st.booleans())
    return {
        "analysis_id": str(draw(st.uuids())),
        "record_id": str(draw(st.uuids())) if signed else None,
        "state": "signed" if signed else "draft",
        "root_cause_category": "engineering" if signed else None,
        "root_cause_statement": draw(_TEXT) if signed else None,
        "conclusion": draw(_TEXT) if signed else None,
        "signed_at": _iso(draw(instants())) if signed else None,
        "created_at": _iso(draw(instants())),
    }


@st.composite
def detail_inputs(draw: st.DrawFn, zone: PrivacyZone) -> DetailInputs:
    subject = draw(_subject(zone))
    records = draw(st.lists(_record(subject), min_size=1, max_size=3))
    evidences = [e.evidence_id for r in records for e in r.evidences]
    granted = draw(st.lists(st.sampled_from(evidences), unique=True)) if evidences else []
    return DetailInputs(
        subject=subject,
        gate=draw(_gate(zone)),
        standard=draw(st.none() | _standard(zone, subject)),
        nodes=draw(st.none() | st.lists(_node(), max_size=2)),
        coverage=draw(st.none() | _coverage()),
        episode=draw(st.none() | _episode(subject)),
        records=records,
        records_limited=draw(st.booleans()),
        read_urls={e: "https://evidencias.example/clip?firma=abc" for e in granted},
        evidence_urls_unavailable=len(granted) < len(evidences),
        classifications=draw(classification_rows()),
        actions=draw(st.lists(_action(), max_size=3)),
        analyses=draw(st.lists(_analysis(), max_size=3)),
    )


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def history_marks(rows: list[dict[str, Any]]) -> list[datetime]:
    return [_present(row["classified_at"]) for row in rows]


def _present(value: Any) -> datetime:
    moment = _instant(value)
    assert moment is not None
    return moment


def marks(inputs: DetailInputs) -> list[datetime]:
    """Cada marca real que el detalle presenta, tantas veces como sale en la respuesta."""
    subject = inputs.subject
    found: list[Any] = [subject["occurred_at_exact"], subject["due_at"]]
    if subject["zone_agreement_revoked"]:
        found.append(subject["zone_agreement_revoked_at"])
    found += [inputs.gate.effective_from, inputs.gate.effective_until]
    if inputs.standard is not None:
        found.append(inputs.standard.effective_from)
        # ``superseded_since`` y el aviso ``later_standard_version``.
        found += [inputs.standard.effective_until] * 2
    for node in inputs.nodes or ():
        found += [node.since, node.last_heartbeat_at]
    if inputs.episode is not None:
        found += [inputs.episode["started_at"], inputs.episode["ended_at"]]
    for record in inputs.records:
        found += [record.received_at, record.occurred_at]
        found += [signal["read_at"] for signal in record.document()["signals"]]
        found += [moment for e in record.evidences for moment in (e.starts_at, e.ends_at)]
    found += history_marks(list(inputs.classifications))
    for action in inputs.actions:
        found += [action["assigned_at"], action["due_at"]]
    for analysis in inputs.analyses:
        found += [analysis["signed_at"], analysis["created_at"]]
    return [moment for moment in (_instant(m) for m in found) if moment is not None]
