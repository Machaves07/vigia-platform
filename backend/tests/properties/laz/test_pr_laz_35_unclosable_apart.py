"""PR-LAZ-35: incerrables separados y ausencia de insumos sin ceros sustitutos."""

import dataclasses
import uuid
from datetime import timedelta

import pytest
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
from tests.properties.laz.test_pr_laz_33_metrics_oracle import calculate
from vigia_platform.accreditation.metrics.compute import compute_metrics, missing
from vigia_platform.loop.domain.enums import MetricKey, MetricStatus, MetricUnit
from vigia_platform.loop.projection.fold import ProjectionInconsistent, ProjectionInput


@given(program_histories())
def test_pr_laz_35_unclosable_never_enters_overdue_or_closures(history: ProgramHistory) -> None:
    values = {m.key: m for m in calculate(history)}
    assert values[MetricKey.OVERDUE].value == sum(s.state == "overdue" for s in history.subjects)
    assert values[MetricKey.PRIMARY_ENGINEERING_CLOSURES].value == sum(
        s.state == "closed"
        and s.engineering
        and s.closed is not None
        and START <= s.closed < END
        and s.closure_eligible
        for s in history.subjects
    )
    assert all(
        m.excluded_unclosable_count == sum(s.state == "unclosable" for s in history.subjects)
        for m in values.values()
    )


@given(program_histories())
def test_pr_laz_35_missing_inputs_are_not_computable_never_zero(history: ProgramHistory) -> None:
    values = compute_metrics(
        history.records,
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        start=START,
        end=END,
        previous_start=PREVIOUS,
        cutoff=END,
        timezone="UTC",
        approved=None,
        adoption_count=None,
        exposure=missing(MetricKey.EXPOSURE, MetricUnit.MINUTES, "Falta el catálogo."),
    )
    for value in values:
        if value.key in (MetricKey.ACTIVATION, MetricKey.ADOPTION, MetricKey.EXPOSURE):
            assert value.status is MetricStatus.NOT_COMPUTABLE
            assert value.value is value.numerator is value.denominator is None
            assert value.not_computable_reason_es


@given(program_histories())
def test_domain_rejects_a_closure_of_an_unclosable_instead_of_returning_metrics(
    history: ProgramHistory,
) -> None:
    for subject in history.subjects:
        if subject.state != "unclosable":
            continue
        seq = len(history.records) + 1
        at = END - timedelta(milliseconds=1)
        closure = ProjectionInput(
            record_id=uuid.UUID(int=6000 + seq),
            organization_id=ORG,
            plant_id=PLANT,
            chain_sequence=seq,
            record_type="closure",
            received_at=at,
            document={
                "subject_id": str(subject.subject_id),
                "subject_kind": subject.kind,
                "action_id": str(uuid.UUID(int=9999)),
                "evidence_entry_ids": [str(uuid.UUID(int=9998))],
                "signed_at": at.isoformat(),
                "signer": {"professional_license": "LIC-SINTETICA"},
            },
        )
        with pytest.raises(ProjectionInconsistent):
            calculate(dataclasses.replace(history, records=(*history.records, closure)))
