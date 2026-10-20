"""Cálculo puro desde el expediente, a un corte histórico, sin leer proyecciones de hoy.

La recepción fija el universo de clasificación y sus parámetros de calendario. La primera
clasificación fija el booleano de plazo: reclasificar no lo altera. El estado al corte se
reconstruye con el pliegue del lazo; ni un cierre ni un vencimiento posterior contamina el mes.
Adopción consume el contador ya definido en BR-LAZ-134 (actividades, no sesiones ni personas).
Vencidos es un conteo, con el subconjunto estrictamente mayor de 30 días (BR-LAZ-113).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.metrics.model import MetricValue
from vigia_platform.loop.calendar import BusinessCalendar, business_deadline, local_date
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.domain.enums import (
    FindingLifecycleState,
    MetricKey,
    MetricStatus,
    MetricUnit,
)
from vigia_platform.loop.projection.fold import PlantProjection, ProjectionInput

_TIMES = {
    "classification": "classified_at",
    "action_assigned": "assigned_at",
    "action_evidence_added": "added_at",
    "closure": "signed_at",
    "unclosable_state_declared": "declared_at",
    "unclosable_review": "reviewed_at",
    "reopening": "reopened_at",
    "action_overdue": "declared_at",
    "loop_configuration_changed": "changed_at",
}


def instant(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def effective_at(item: ProjectionInput) -> datetime:
    field = _TIMES.get(item.record_type)
    return instant(item.document[field]) if field is not None else item.received_at


def missing(key: MetricKey, unit: MetricUnit, reason: str, excluded: int = 0) -> MetricValue:
    return MetricValue(
        key=key,
        unit=unit,
        status=MetricStatus.NOT_COMPUTABLE,
        not_computable_reason_es=reason,
        excluded_unclosable_count=excluded,
        detail_code=AccreditationDetailCode.METRIC_NOT_COMPUTABLE,
    )


def compute_metrics(
    records: Sequence[ProjectionInput],
    *,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    start: datetime,
    end: datetime,
    previous_start: datetime,
    cutoff: datetime,
    timezone: str,
    approved: bool | None,
    adoption_count: int | None,
    exposure: MetricValue,
) -> tuple[MetricValue, ...]:
    """Las siete métricas de una zona, sin desagregación adicional ni segunda verdad."""
    history = sorted(
        (
            r
            for r in records
            if r.organization_id == organization_id
            and r.plant_id == plant_id
            and r.received_at < end
            and effective_at(r) < end
            and effective_at(r) <= cutoff
        ),
        key=lambda r: r.chain_sequence,
    )
    projection = PlantProjection(organization_id, plant_id)
    receipts: dict[uuid.UUID, datetime] = {}
    deadlines: dict[uuid.UUID, datetime] = {}
    first_flags: dict[uuid.UUID, bool] = {}
    defaults = LoopSettings.defaults()
    deadline_days = defaults["classification_deadline_business_days"]
    calendar = defaults.business_calendar
    engineering = 0
    closed_families: list[tuple[str, datetime]] = []
    evidence: set[str] = set()
    for item in history:
        doc = item.document
        if item.record_type == "loop_configuration_changed":
            for setting in doc["new_values"]:
                if setting["parameter"] == "classification_deadline_business_days":
                    deadline_days = int(setting["integer_value"])
                elif setting["parameter"] == "business_calendar":
                    calendar = BusinessCalendar.from_document(setting["business_calendar"])
            continue
        if item.record_type == "finding_received":
            subject = uuid.UUID(str(doc["episode"]["episode_id"]))
            if subject not in receipts:
                receipts[subject] = item.received_at
                deadlines[subject] = business_deadline(
                    item.received_at, deadline_days, calendar, timezone
                )
        if item.record_type == "classification":
            subject = uuid.UUID(str(doc["subject_id"]))
            if subject in deadlines:
                first_flags.setdefault(subject, instant(doc["classified_at"]) < deadlines[subject])
        if item.record_type == "action_evidence_added":
            evidence.add(str(doc["evidence_entry_id"]))
        if item.record_type == "closure":
            subject = uuid.UUID(str(doc["subject_id"]))
            finding = projection.findings.get(subject)
            at = instant(doc["signed_at"])
            # La acción vigente y la evidencia se comprueban antes de plegar el cierre.
            eligible = (
                finding is not None
                and finding.zone_id == zone_id
                and finding.state is not FindingLifecycleState.UNCLOSABLE
                and str(finding.current_action_id) == str(doc["action_id"])
                and bool(set(map(str, doc["evidence_entry_ids"])) & evidence)
            )
            if eligible and finding is not None:
                if start <= at < end and finding.current_action_type == "engineering":
                    engineering += 1
                if previous_start <= at < start:
                    closed_families.append((finding.family, at))
        projection.apply(item)
    findings = [f for f in projection.findings.values() if f.zone_id == zone_id]
    excluded = sum(f.state is FindingLifecycleState.UNCLOSABLE for f in findings)
    overdue = [f for f in findings if f.state is FindingLifecycleState.OVERDUE]
    received = [
        f.subject_id
        for f in findings
        if f.subject_id in receipts and start <= receipts[f.subject_id] < end
    ]
    episodes = [
        e
        for e in projection.episodes.values()
        if e.zone_id == zone_id and start <= e.started_at < end and e.started_at < cutoff
    ]
    recurrence = sum(
        any(e.family == family and e.started_at > closed for family, closed in closed_families)
        for e in episodes
    )

    def count(key: MetricKey, value: int) -> MetricValue:
        return MetricValue(
            key=key,
            status=MetricStatus.COMPUTED,
            unit=MetricUnit.COUNT,
            value=float(value),
            excluded_unclosable_count=excluded,
        )

    def ratio(key: MetricKey, numerator: int, denominator: int) -> MetricValue:
        if not denominator:
            return missing(key, MetricUnit.RATIO, "El universo del periodo está vacío.", excluded)
        return MetricValue(
            key=key,
            status=MetricStatus.COMPUTED,
            unit=MetricUnit.RATIO,
            value=numerator / denominator,
            numerator=numerator,
            denominator=denominator,
            excluded_unclosable_count=excluded,
        )

    older = sum(
        f.due_at is not None
        and (
            local_date(cutoff - timedelta(microseconds=1), timezone)
            - local_date(f.due_at, timezone)
        ).days
        > 30
        for f in overdue
    )
    return (
        count(MetricKey.PRIMARY_ENGINEERING_CLOSURES, engineering),
        missing(
            MetricKey.ACTIVATION,
            MetricUnit.RATIO,
            "No está disponible la compuerta de uso.",
            excluded,
        )
        if approved is None
        else ratio(MetricKey.ACTIVATION, int(approved), 1),
        missing(
            MetricKey.ADOPTION,
            MetricUnit.COUNT,
            "No está disponible el contador de actividad.",
            excluded,
        )
        if adoption_count is None
        else count(MetricKey.ADOPTION, adoption_count),
        ratio(
            MetricKey.CLASSIFIED_IN_TIME,
            sum(first_flags.get(s, False) for s in received),
            len(received),
        ),
        count(MetricKey.OVERDUE, len(overdue)).model_copy(update={"over_30_days_count": older}),
        count(MetricKey.RECURRENCE, recurrence),
        exposure.model_copy(update={"excluded_unclosable_count": excluded}),
    )
