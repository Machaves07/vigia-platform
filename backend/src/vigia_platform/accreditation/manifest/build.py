"""Composición pura del manifiesto (LC-LAZ-24; DE §3.2; BR-LAZ-88 a 99; PAT-LAZ-SEG-02).

- ``count_states(findings)``: los conteos por estado de **todos** los hallazgos del periodo
  (BR-LAZ-89): abiertos (``received``, ``in_review``, ``open``, ``action_assigned`` y
  ``overdue``), cerrados, incerrables, descartados, suspendidos por reclasificación y, aparte, los
  que llevan ``evidence_flagged`` (BR-LAZ-94). Los cinco primeros suman ``finding_count``.
- ``build_manifest(inputs)``: el ``ExportManifest`` con todo ordenado de forma estable (el mismo
  insumo da los mismos bytes canónicos: PR-LAZ-49), los avisos fijos que procedan (agregación si
  alguna zona es unipersonal, revocación si alguna compuerta de uso se revocó en el periodo) y las
  comprobaciones que hacen imposible un manifiesto que esconda algo: una sola organización por
  construcción, un resumen de cobertura por zona que **suma exactamente** la duración del periodo
  (nunca se omite un tramo, BR-LAZ-93), hallazgos solo de las zonas pedidas y entradas ausentes
  solo con su motivo fijo.
- ``manifest_json(manifest)``: la forma JSON (marcas ISO 8601 UTC con milisegundos y ``Z``;
  el periodo, además, en la hora local de la planta).

Módulo puro: sin FastAPI, sin SQLAlchemy, sin E/S y sin hora del sistema.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from vigia_platform.accreditation.manifest.model import (
    ABSENT_KINDS,
    AGGREGATION_NOTICE_ES,
    ASSIGNMENT_HISTORY_UNAVAILABLE_ES,
    DOCUMENT_ABSENT_ES,
    MANIFEST_VERSION,
    REVOCATION_NOTICE_ES,
    VERIFIER_ABSENT_ES,
    WITH_CLIPS_NOTICE_ES,
    WITHOUT_CLIPS_NOTICE_ES,
    AbsentEntry,
    AgreementEntry,
    ArtifactEntry,
    AssignmentEntry,
    CheckpointEntry,
    CoverageZoneSummary,
    DeclaredAttachment,
    DeclaredClip,
    ExportManifest,
    FindingSummary,
    GateIntervalEntry,
    ManifestInputs,
    RegressionPeriod,
    RevokedGate,
    StandardCited,
    StateCounts,
    ZoneEntry,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState, PackageForm

__all__ = [
    "ABSENT_REASONS",
    "OPEN_STATES",
    "ManifestInvalid",
    "build_manifest",
    "count_states",
    "manifest_json",
]

S = FindingLifecycleState

OPEN_STATES: Final = frozenset({S.RECEIVED, S.IN_REVIEW, S.OPEN, S.ACTION_ASSIGNED, S.OVERDUE})
"""Lo que el manifiesto cuenta como abierto: todo hallazgo que aún no salió del lazo."""

ABSENT_REASONS: Final[Mapping[str, str]] = {
    "document": DOCUMENT_ABSENT_ES,
    "verifier": VERIFIER_ABSENT_ES,
}
"""El motivo fijo de cada entrada que puede declararse ausente."""

_MS: Final = timedelta(milliseconds=1)


class ManifestInvalid(ValueError):
    """Los insumos no permiten un manifiesto honesto (algo faltaría o sobraría en silencio)."""


def _stamp(moment: datetime) -> str:
    if moment.utcoffset() is None:
        raise ManifestInvalid("marca sin zona horaria")
    utc = moment.astimezone(UTC)
    utc = utc.replace(microsecond=utc.microsecond // 1000 * 1000)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_stamp(moment: datetime | None) -> str | None:
    return None if moment is None else _stamp(moment)


def _local(moment: datetime, timezone: str) -> str:
    return moment.astimezone(ZoneInfo(timezone)).isoformat(timespec="milliseconds")


def _id(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


# --- conteos -------------------------------------------------------------------------------------


def count_states(findings: Iterable[FindingSummary]) -> StateCounts:
    """Conteos por estado (BR-LAZ-89) y de evidencia marcada (BR-LAZ-94)."""
    by_state: Counter[str] = Counter()
    flagged = 0
    for finding in findings:
        by_state[FindingLifecycleState(finding.lifecycle_state).value] += 1
        flagged += finding.evidence_flagged
    total = sum(by_state.values())
    return StateCounts(
        finding_count=total,
        open_finding_count=sum(by_state[state.value] for state in OPEN_STATES),
        closed_finding_count=by_state[S.CLOSED.value],
        unclosable_count=by_state[S.UNCLOSABLE.value],
        discarded_count=by_state[S.DISCARDED.value],
        suspended_count=by_state[S.SUSPENDED_BY_RECLASSIFICATION.value],
        evidence_flagged_count=flagged,
        by_state={state.value: by_state[state.value] for state in FindingLifecycleState},
    )


# --- composición ---------------------------------------------------------------------------------


def _check(inputs: ManifestInputs) -> None:
    form = PackageForm(inputs.package_form)
    if inputs.period_to <= inputs.period_from:
        raise ManifestInvalid("el periodo termina después de empezar")
    zone_ids = [zone.zone_id for zone in inputs.zones]
    if not zone_ids or len(set(zone_ids)) != len(zone_ids):
        raise ManifestInvalid("el manifiesto declara cada zona pedida una sola vez")
    zones = set(zone_ids)
    if any(finding.zone_id not in zones for finding in inputs.findings):
        raise ManifestInvalid("un hallazgo de una zona que no se pidió")
    subjects = [finding.subject_id for finding in inputs.findings]
    if len(set(subjects)) != len(subjects):
        raise ManifestInvalid("un hallazgo aparece dos veces")
    covered = [summary.zone_id for summary in inputs.coverage]
    if sorted(covered, key=str) != sorted(zone_ids, key=str):
        raise ManifestInvalid("cada zona lleva exactamente un resumen de cobertura")
    period_ms = (inputs.period_to - inputs.period_from) // _MS
    for summary in inputs.coverage:
        if summary.total_ms != period_ms:
            # BR-LAZ-93: un resumen que no suma el periodo escondería un tramo.
            raise ManifestInvalid("el resumen de cobertura no suma la duración del periodo")
    declared = (
        *(item.zone_id for item in inputs.regression_periods),
        *(item.zone_id for item in inputs.revoked_gates),
        *(item.zone_id for item in inputs.standards_cited),
        *(item.zone_id for item in inputs.attachments),
    )
    if any(zone_id not in zones for zone_id in declared):
        raise ManifestInvalid("una declaración de una zona que no se pidió")
    for clip in inputs.clips:
        if clip.zone_id not in zones:
            raise ManifestInvalid("un clip de una zona que no se pidió")
        if (clip.path is None) != (form is PackageForm.WITHOUT_CLIPS):
            raise ManifestInvalid("los clips llevan bytes solo en la forma con clips")
    kinds = [entry.kind for entry in inputs.absent]
    if len(set(kinds)) != len(kinds) or any(kind not in ABSENT_KINDS for kind in kinds):
        raise ManifestInvalid("entrada ausente desconocida o repetida")
    for entry in inputs.absent:
        if entry.reason_es != ABSENT_REASONS[entry.kind]:
            raise ManifestInvalid("una entrada ausente solo lleva su motivo fijo")
    present = {artifact.kind for artifact in inputs.artifacts}
    for kind in ABSENT_KINDS:
        if (kind in present) == (kind in kinds):
            # Nada falta en silencio: o está entre los artefactos o se declara ausente.
            raise ManifestInvalid(f"«{kind}» debe estar presente o declararse ausente, no ambos")
    paths = [artifact.path for artifact in inputs.artifacts]
    if len(set(paths)) != len(paths):
        raise ManifestInvalid("ruta de artefacto repetida")


def _overlaps(end: datetime | None, period_from: datetime) -> bool:
    return end is None or end > period_from


def build_manifest(inputs: ManifestInputs) -> ExportManifest:
    """El manifiesto de ``inputs``, ordenado de forma estable y con los avisos que procedan."""
    _check(inputs)
    form = PackageForm(inputs.package_form)
    zones = tuple(sorted(inputs.zones, key=lambda zone: str(zone.zone_id)))
    aggregated = tuple(zone.zone_id for zone in zones if zone.single_occupancy)
    revoked = tuple(
        sorted(
            (
                gate
                for gate in inputs.revoked_gates
                if gate.since < inputs.period_to and _overlaps(gate.until, inputs.period_from)
            ),
            key=lambda gate: (str(gate.zone_id), gate.since),
        )
    )
    return ExportManifest(
        export_id=inputs.export_id,
        package_form=form.value,
        package_form_notice_es=(
            WITHOUT_CLIPS_NOTICE_ES if form is PackageForm.WITHOUT_CLIPS else WITH_CLIPS_NOTICE_ES
        ),
        organization=inputs.organization,
        plant=inputs.plant,
        period_from=inputs.period_from,
        period_to=inputs.period_to,
        generated_at=inputs.generated_at,
        generated_by=inputs.generated_by,
        counts=count_states(inputs.findings),
        zones=zones,
        coverage_summary=tuple(sorted(inputs.coverage, key=lambda item: str(item.zone_id))),
        regression_periods=tuple(
            sorted(inputs.regression_periods, key=lambda item: (str(item.zone_id), item.starts_at))
        ),
        revoked_gates=revoked,
        standards_cited=tuple(
            sorted(
                inputs.standards_cited,
                key=lambda item: (str(item.zone_id), str(item.standard_id), item.version),
            )
        ),
        clips=tuple(sorted(inputs.clips, key=lambda clip: str(clip.evidence_id))),
        attachments=tuple(
            sorted(inputs.attachments, key=lambda item: (str(item.attachment_id), item.path))
        ),
        aggregated_zone_ids=aggregated,
        artifacts=tuple(inputs.artifacts),
        absent=tuple(sorted(inputs.absent, key=lambda entry: entry.kind)),
        checkpoints=tuple(
            sorted(inputs.checkpoints, key=lambda item: (item.kind, str(item.plant_id or "")))
        ),
        checkpoint_public_keys=tuple(
            sorted(inputs.checkpoint_public_keys, key=lambda key: key["key_id"])
        ),
        verifier_version=inputs.verifier_version,
        aggregation_notice_es=AGGREGATION_NOTICE_ES if aggregated else None,
        revocation_notice_es=REVOCATION_NOTICE_ES if revoked else None,
    )


# --- JSON ----------------------------------------------------------------------------------------


def _gate(entry: GateIntervalEntry) -> dict[str, Any]:
    return {
        "gate": entry.gate,
        "status": entry.status,
        "effective_from": _stamp(entry.effective_from),
        "effective_until": _optional_stamp(entry.effective_until),
        "decided_by": str(entry.decided_by),
        "reason_es": entry.reason_es,
        "record_id": _id(entry.record_id),
    }


def _agreement(entry: AgreementEntry) -> dict[str, Any]:
    return {
        "agreement_id": str(entry.agreement_id),
        "record_id": str(entry.record_id),
        "status": entry.status,
        "approved_at": _stamp(entry.approved_at),
        "approved_by": str(entry.approved_by),
        "state_in_period": entry.state_in_period,
        "since": _stamp(entry.since),
        "replaces_agreement_id": _id(entry.replaces_agreement_id),
        "signatories": [
            {
                "role": signatory.role,
                "user_id": str(signatory.user_id),
                "confirmed_at": _optional_stamp(signatory.confirmed_at),
            }
            for signatory in entry.signatories
        ],
        "document_sha256": entry.document_sha256,
    }


def _assignment(entry: AssignmentEntry) -> dict[str, Any]:
    return {
        "node_id": _id(entry.node_id),
        "assigned_at": _stamp(entry.assigned_at),
        "unassigned_at": _optional_stamp(entry.unassigned_at),
        "replaces_node_id": _id(entry.replaces_node_id),
    }


def _zone(zone: ZoneEntry) -> dict[str, Any]:
    document: dict[str, Any] = {
        "zone_id": str(zone.zone_id),
        "code": zone.code,
        "name": zone.name,
        "gate_intervals": [
            _gate(entry)
            for entry in sorted(
                zone.gate_intervals, key=lambda entry: (entry.gate, entry.effective_from)
            )
        ],
        "agreements": [
            _agreement(entry)
            for entry in sorted(
                zone.agreements, key=lambda entry: (entry.approved_at, str(entry.agreement_id))
            )
        ],
        "single_occupancy": zone.single_occupancy,
        "aggregation_window_minutes": zone.aggregation_window_minutes,
    }
    if zone.node_assignments is None:
        document["node_assignments"] = None
        document["node_assignments_absent_reason_es"] = ASSIGNMENT_HISTORY_UNAVAILABLE_ES
    else:
        document["node_assignments"] = [
            _assignment(entry)
            for entry in sorted(zone.node_assignments, key=lambda entry: entry.assigned_at)
        ]
    return document


def _coverage(summary: CoverageZoneSummary) -> dict[str, Any]:
    return {
        "zone_id": str(summary.zone_id),
        "observable_ms": summary.observable_ms,
        "degraded_ms": summary.degraded_ms,
        "not_observable_ms": summary.not_observable_ms,
        "no_communication_ms": summary.no_communication_ms,
        "never_reported_ms": summary.never_reported_ms,
        "zone_not_active_ms": summary.zone_not_active_ms,
        "not_elapsed_ms": summary.not_elapsed_ms,
        "unobserved_ms": summary.unobserved_ms,
        "total_ms": summary.total_ms,
        "interval_count": summary.interval_count,
    }


def _regression(item: RegressionPeriod) -> dict[str, Any]:
    return {
        "zone_id": str(item.zone_id),
        "from": _stamp(item.starts_at),
        "to": _optional_stamp(item.ends_at),
        "cause": item.cause,
    }


def _revoked(item: RevokedGate) -> dict[str, Any]:
    return {
        "zone_id": str(item.zone_id),
        "since": _stamp(item.since),
        "until": _optional_stamp(item.until),
        "reason_es": item.reason_es,
        "record_id": _id(item.record_id),
    }


def _standard(item: StandardCited) -> dict[str, Any]:
    return {
        "zone_id": str(item.zone_id),
        "standard_id": str(item.standard_id),
        "version": item.version,
        "title_es": item.title_es,
        "declared_text": item.declared_text,
        "declared_text_sha256": item.declared_text_sha256,
    }


def _clip(item: DeclaredClip) -> dict[str, Any]:
    return {
        "evidence_id": str(item.evidence_id),
        "clip_id": str(item.clip_id),
        "zone_id": str(item.zone_id),
        "subject_id": str(item.subject_id),
        "record_id": str(item.record_id),
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "duration_ms": item.duration_ms,
        "media_kind": item.media_kind,
        "included": item.path is not None,
        "path": item.path,
    }


def _attachment(item: DeclaredAttachment) -> dict[str, Any]:
    return {
        "attachment_id": str(item.attachment_id),
        "subject_id": str(item.subject_id),
        "zone_id": str(item.zone_id),
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "content_type": item.content_type,
        "declaration": "no_identifiable_person_declared",
        "path": item.path,
    }


def _artifact(item: ArtifactEntry) -> dict[str, Any]:
    return {
        "path": item.path,
        "kind": item.kind,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "volume": item.volume,
    }


def _absent(item: AbsentEntry) -> dict[str, Any]:
    return {"kind": item.kind, "reason_es": item.reason_es}


def _checkpoint(item: CheckpointEntry) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "plant_id": _id(item.plant_id),
        "entry_id": str(item.entry_id),
        "sequence": item.sequence,
        "entry_hash": item.entry_hash,
        "content": dict(item.content),
    }


def manifest_json(manifest: ExportManifest) -> dict[str, Any]:
    """La forma JSON del manifiesto (lo que ``ArchiveWriter.finish`` canonicaliza)."""
    counts = manifest.counts
    timezone = manifest.plant.timezone
    document: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "export_id": str(manifest.export_id),
        "package_form": manifest.package_form,
        "package_form_notice_es": manifest.package_form_notice_es,
        "organization": {
            "organization_id": str(manifest.organization.organization_id),
            "code": manifest.organization.code,
            "name": manifest.organization.name,
        },
        "plant": {
            "plant_id": str(manifest.plant.plant_id),
            "code": manifest.plant.code,
            "name": manifest.plant.name,
            "country": manifest.plant.country,
            "timezone": timezone,
        },
        "period": {
            "from": _stamp(manifest.period_from),
            "to": _stamp(manifest.period_to),
            "from_local": _local(manifest.period_from, timezone),
            "to_local": _local(manifest.period_to, timezone),
            "timezone": timezone,
        },
        "generated_at": _stamp(manifest.generated_at),
        "generated_by": {
            "user_id": manifest.generated_by.user_id,
            "display_name": manifest.generated_by.display_name,
            "role_in_use": manifest.generated_by.role_in_use,
        },
        "finding_count": counts.finding_count,
        "open_finding_count": counts.open_finding_count,
        "closed_finding_count": counts.closed_finding_count,
        "unclosable_count": counts.unclosable_count,
        "discarded_count": counts.discarded_count,
        "suspended_count": counts.suspended_count,
        "evidence_flagged_count": counts.evidence_flagged_count,
        "state_counts": dict(counts.by_state),
        "zones": [_zone(zone) for zone in manifest.zones],
        "coverage_summary": [_coverage(item) for item in manifest.coverage_summary],
        "regression_periods": [_regression(item) for item in manifest.regression_periods],
        "revoked_gates": [_revoked(item) for item in manifest.revoked_gates],
        "standards_cited": [_standard(item) for item in manifest.standards_cited],
        "clips": [_clip(item) for item in manifest.clips],
        "attachments": [_attachment(item) for item in manifest.attachments],
        "aggregated_zone_ids": [str(zone) for zone in manifest.aggregated_zone_ids],
        "artifacts": [_artifact(item) for item in manifest.artifacts],
        "absent_entries": [_absent(item) for item in manifest.absent],
        "checkpoints": [_checkpoint(item) for item in manifest.checkpoints],
        "checkpoint_public_keys": [dict(key) for key in manifest.checkpoint_public_keys],
        "verifier_version": manifest.verifier_version,
        "coverage_notice_es": manifest.coverage_notice_es,
        "anonymization_statement_es": manifest.anonymization_statement_es,
        "evidence_flagged_notice_es": manifest.evidence_flagged_notice_es,
        "regression_notice_es": manifest.regression_notice_es,
    }
    if manifest.aggregation_notice_es is not None:
        document["aggregation_notice_es"] = manifest.aggregation_notice_es
    if manifest.revocation_notice_es is not None:
        document["revocation_notice_es"] = manifest.revocation_notice_es
    return document
