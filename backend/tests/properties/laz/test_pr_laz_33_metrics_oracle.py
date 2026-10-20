"""PR-LAZ-33: fuerza bruta sobre hechos generados, independiente del pliegue del cálculo."""

import dataclasses
import uuid
from datetime import timedelta

from hypothesis import given

from tests.properties.laz.strategies.program import (
    END,
    ORG,
    PLANT,
    PREVIOUS,
    START,
    ZONE,
    ProgramHistory,
    program_histories,
)
from vigia_platform.accreditation.metrics.compute import compute_metrics
from vigia_platform.accreditation.metrics.model import MetricValue
from vigia_platform.loop.domain.enums import MetricKey, MetricStatus, MetricUnit
from vigia_platform.loop.projection.fold import ProjectionInput


def calculate(history: ProgramHistory) -> tuple[MetricValue, ...]:
    return compute_metrics(
        history.records,
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        start=START,
        end=END,
        previous_start=PREVIOUS,
        cutoff=END,
        timezone="UTC",
        approved=history.approved,
        adoption_count=history.adoption,
        exposure=MetricValue(
            key=MetricKey.EXPOSURE,
            status=MetricStatus.COMPUTED,
            unit=MetricUnit.MINUTES,
            value=history.exposure,
            excluded_unclosable_count=0,
        ),
    )


@given(program_histories())
def test_pr_laz_33_seven_metrics_match_the_independent_oracle(history: ProgramHistory) -> None:
    values = {v.key: v for v in calculate(history)}
    subjects = history.subjects
    excluded = sum(s.state == "unclosable" for s in subjects)
    assert len(values) == 7
    assert all(v.excluded_unclosable_count == excluded for v in values.values())
    assert values[MetricKey.PRIMARY_ENGINEERING_CLOSURES].value == sum(
        s.closed is not None and START <= s.closed < END and s.engineering for s in subjects
    )
    assert values[MetricKey.ACTIVATION].value == int(history.approved)
    assert values[MetricKey.ADOPTION].value == history.adoption
    received = [s for s in subjects if s.kind == "finding" and START <= s.received < END]
    timely = 0
    for s in received:
        # Enumeración de días, no business_deadline ni el booleano del cálculo.
        day = s.received.date()
        remaining = 3
        while remaining:
            day += timedelta(days=1)
            if day.isoweekday() <= 5:
                remaining -= 1
        if s.first_classified is not None and s.first_classified.date() <= day:
            timely += 1
    metric = values[MetricKey.CLASSIFIED_IN_TIME]
    if received:
        assert metric.value == timely / len(received)
        assert (metric.numerator, metric.denominator) == (timely, len(received))
    else:
        assert metric.status is MetricStatus.NOT_COMPUTABLE
        assert metric.value is None
    assert values[MetricKey.OVERDUE].value == sum(s.state == "overdue" for s in subjects)
    assert values[MetricKey.OVERDUE].over_30_days_count == sum(
        s.state == "overdue" and ((END - timedelta(microseconds=1)).date() - s.due.date()).days > 30
        for s in subjects
    )
    recurrence = 0
    for episode in subjects:
        if episode.kind != "finding" or not START <= episode.received < END:
            continue
        matches = [
            closed
            for closed in subjects
            if closed.closed is not None
            and PREVIOUS <= closed.closed < START
            and closed.family == episode.family
            and episode.received > closed.closed
        ]
        recurrence += bool(matches)
    assert values[MetricKey.RECURRENCE].value == recurrence
    assert values[MetricKey.EXPOSURE].value == history.exposure


@given(program_histories())
def test_first_classification_flag_survives_reclassification_and_future_records(
    history: ProgramHistory,
) -> None:
    original = {v.key: v for v in calculate(history)}[MetricKey.CLASSIFIED_IN_TIME]
    records = list(history.records)
    for subject in history.subjects:
        if subject.state not in ("closed", "unclosable"):
            continue
        seq = len(records) + 1
        at = END - timedelta(milliseconds=1)
        records.append(
            ProjectionInput(
                record_id=uuid.UUID(int=5000 + seq),
                organization_id=ORG,
                plant_id=PLANT,
                chain_sequence=seq,
                record_type="classification",
                received_at=at,
                document={
                    "subject_id": str(subject.subject_id),
                    "subject_kind": "finding",
                    "classified_at": at.isoformat(),
                    "outcome": "false_positive",
                    "reason_category": "no_person_present",
                    "reinforced_reason": True,
                },
            )
        )
    later = dataclasses.replace(history, records=tuple(records))
    assert {v.key: v for v in calculate(later)}[MetricKey.CLASSIFIED_IN_TIME] == original
    future = tuple(dataclasses.replace(r, received_at=END + timedelta(days=1)) for r in records)
    # Ningún registro recibido después del cierre modifica los estados históricos del periodo.
    future_result = calculate(dataclasses.replace(history, records=future))
    assert {v.key: v for v in future_result}[
        MetricKey.CLASSIFIED_IN_TIME
    ].status is MetricStatus.NOT_COMPUTABLE
