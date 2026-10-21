"""PR-LAZ-50: fronteras exactas de validez y huella canónica."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.program import (
    END,
    ORG,
    PLANT,
    START,
    ZONE,
    ProgramHistory,
    program_histories,
)
from tests.properties.laz.test_pr_laz_33_metrics_oracle import calculate
from vigia_platform.accreditation.metrics.model import ProgramMetricSnapshot, reusable
from vigia_platform.accreditation.metrics.service import inputs_digest
from vigia_platform.accreditation.metrics.task import closed_periods


@given(program_histories(), st.integers(0, 1_000_000), st.integers(-1, 901))
def test_pr_laz_50_cache_boundaries_and_digest(
    history: ProgramHistory, head: int, seconds: int
) -> None:
    digest = inputs_digest(head=head, zones=(ZONE,), period="2026-03", adoption=history.adoption)
    snapshot = ProgramMetricSnapshot(
        snapshot_id=ZONE,
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        period="2026-03",
        period_from=START,
        period_to=END,
        values=calculate(history),
        materialized_at=START,
        recomputed_at=START,
        inputs_digest=digest,
    )
    now = START + timedelta(seconds=seconds)
    assert reusable(snapshot, digest, now, definitive=False) == (0 <= seconds < 900)
    changed = inputs_digest(
        head=head + 1, zones=(ZONE,), period="2026-03", adoption=history.adoption
    )
    assert not reusable(snapshot, changed, now, definitive=False)
    assert not reusable(snapshot, digest, now, definitive=True)
    final = snapshot.model_copy(update={"recomputed_at": None})
    assert reusable(final, changed, END + timedelta(days=1), definitive=True)
    assert (
        inputs_digest(head=head, zones=(ZONE, ZONE), period="2026-03", adoption=history.adoption)
        == digest
    )


@pytest.mark.parametrize(
    "timezone,expected",
    [
        ("America/Bogota", ("2026-01",)),
        ("Europe/Madrid", ("2026-01", "2026-02")),
    ],
)
def test_month_close_uses_the_plant_calendar(timezone: str, expected: tuple[str, ...]) -> None:
    created = datetime(2026, 1, 15, tzinfo=UTC)
    now = datetime(2026, 3, 1, 3, tzinfo=UTC)
    assert closed_periods(created, now, timezone) == expected


@pytest.mark.parametrize(
    "milliseconds,expected",
    [
        (-1, False),
        (0, True),
        (899999, True),
        (900000, False),
        (900001, False),
    ],
)
def test_exact_freshness_boundary(milliseconds: int, expected: bool) -> None:
    empty = ProgramHistory((), (), False, 0, 0.0)
    snapshot = ProgramMetricSnapshot(
        snapshot_id=ZONE,
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        period="2026-03",
        period_from=START,
        period_to=END,
        values=calculate(empty),
        materialized_at=START,
        recomputed_at=START,
        inputs_digest="a" * 64,
    )
    assert (
        reusable(snapshot, "a" * 64, START + timedelta(milliseconds=milliseconds), definitive=False)
        is expected
    )
