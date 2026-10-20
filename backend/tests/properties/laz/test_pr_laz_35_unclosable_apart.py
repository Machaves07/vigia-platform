"""PR-LAZ-35: incerrables separados y ausencia de insumos sin ceros sustitutos."""

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


@given(program_histories())
def test_pr_laz_35_unclosable_never_enters_overdue_or_closures(history: ProgramHistory) -> None:
    values = {m.key: m for m in calculate(history)}
    assert values[MetricKey.OVERDUE].value == sum(s.state == "overdue" for s in history.subjects)
    assert values[MetricKey.PRIMARY_ENGINEERING_CLOSURES].value == sum(
        s.state == "closed" and s.engineering and s.closed is not None and START <= s.closed < END
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
