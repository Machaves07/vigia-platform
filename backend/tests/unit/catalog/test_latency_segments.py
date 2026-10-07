"""Latencia del acta en cuatro tramos que no se restan (TASK-216; NFR-GOB-70, PAT-GOB-REN-08).

- Estadísticos de rango más cercano: cada cifra es una medida real.
- La resta solo existe entre marcas del **mismo** reloj: mezclar relojes es ``ClockMismatch``.
- Un tramo sin muestras queda «no medido» (nulo y en ``not_measured``); la suma está marcada como
  orientativa.
- Propiedad (Hypothesis): desplazar el reloj del navegador o el de la plataforma una cantidad
  arbitraria no cambia ningún tramo; ninguno depende de otro reloj.

Funciones puras, sin base. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.catalog.domain.latency import (
    MAX_LATENCY_MS,
    ClockMismatch,
    ExposureSample,
    LatencyReport,
    MeasuredBy,
    Stamp,
    TrancheName,
    latency_report,
    measure,
    nearest_rank,
)
from vigia_platform.catalog.record_types import LatencyV2

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
PLATFORM = MeasuredBy.PLATFORM
BROWSER = MeasuredBy.BROWSER


def _intervals(clock: MeasuredBy, durations: list[int], base: datetime = T0) -> list[Any]:
    return [
        (
            Stamp(clock, base + timedelta(seconds=index)),
            Stamp(clock, base + timedelta(seconds=index, milliseconds=duration)),
        )
        for index, duration in enumerate(durations)
    ]


def test_nearest_rank_returns_measured_values() -> None:
    values = list(range(1, 101))
    assert (nearest_rank(values, 50), nearest_rank(values, 95), nearest_rank(values, 100)) == (
        50,
        95,
        100,
    )
    assert nearest_rank([7], 95) == 7
    assert nearest_rank([10, 20], 50) == 10
    for ordered, percentile in (([], 50), ([1], 0), ([1], 101)):
        with pytest.raises(ValueError, match="percentil"):
            nearest_rank(ordered, percentile)


def test_a_tranche_has_median_p95_max_and_repetitions() -> None:
    tranche = measure(_intervals(PLATFORM, list(range(1, 101))), PLATFORM)
    assert tranche is not None
    assert (tranche.median_ms, tranche.p95_ms, tranche.max_ms, tranche.repetitions) == (
        50,
        95,
        100,
        100,
    )
    assert tranche.measured_by is PLATFORM


def test_without_intervals_the_tranche_is_not_measured() -> None:
    assert measure([], PLATFORM) is None


def test_negative_or_over_an_hour_intervals_are_not_measures() -> None:
    backwards = (Stamp(PLATFORM, T0), Stamp(PLATFORM, T0 - timedelta(milliseconds=5)))
    too_long = (
        Stamp(PLATFORM, T0),
        Stamp(PLATFORM, T0 + timedelta(milliseconds=MAX_LATENCY_MS + 1)),
    )
    edge = (Stamp(PLATFORM, T0), Stamp(PLATFORM, T0 + timedelta(milliseconds=MAX_LATENCY_MS)))
    assert measure([backwards, too_long], PLATFORM) is None
    tranche = measure([backwards, edge], PLATFORM)
    assert tranche is not None and (tranche.max_ms, tranche.repetitions) == (MAX_LATENCY_MS, 1)


@pytest.mark.parametrize(
    ("start", "end"),
    [(BROWSER, PLATFORM), (PLATFORM, BROWSER), (MeasuredBy.NODE, PLATFORM)],
)
def test_marks_of_two_clocks_are_never_subtracted(start: MeasuredBy, end: MeasuredBy) -> None:
    with pytest.raises(ClockMismatch):
        Stamp(start, T0).until(Stamp(end, T0 + timedelta(seconds=1)))
    with pytest.raises(ClockMismatch):
        measure(
            [(Stamp(start, T0), Stamp(end, T0))], PLATFORM if start is not PLATFORM else BROWSER
        )


def test_the_report_keeps_the_four_tranches_apart_and_marks_the_sum_as_indicative() -> None:
    report = latency_report(
        beacon_latency_ms_p95=180,
        upload=_intervals(PLATFORM, [400] * 3),
        exposure=[],
        served=_intervals(PLATFORM, [150, 250, 350]),
    )
    document = report.to_json()
    assert document["node_tranche"] == {
        "median_ms": None,
        "p95_ms": 180,
        "max_ms": None,
        "repetitions": None,
        "measured_by": "installer",
    }
    assert document["platform_tranche"]["median_ms"] == 400
    assert document["exposure_tranche"] is None
    assert document["served_tranche"]["median_ms"] == 250
    assert document["not_measured"] == [TrancheName.EXPOSURE.value]
    assert document["indicative"] is True
    assert document["indicative_sum_median_ms"] == 400 + 250  # el tramo 1 no trae mediana
    assert document["indicative_sum_p95_ms"] == 180 + 400 + 350
    # La forma es la del acta v2 (con las repeticiones contadas que añade el cierre).
    LatencyV2.model_validate_json(json.dumps({**document, "repetitions_counted": 104}))


def test_with_nothing_measured_only_the_installer_tranche_remains() -> None:
    report = latency_report(beacon_latency_ms_p95=0, upload=[], exposure=[], served=[])
    assert report.not_measured == (
        TrancheName.PLATFORM,
        TrancheName.EXPOSURE,
        TrancheName.SERVED,
    )
    assert (report.indicative_sum_median_ms, report.indicative_sum_p95_ms) == (None, 0)


@pytest.mark.parametrize("beacon", [-1, MAX_LATENCY_MS + 1, 1.5, True, "180"])
def test_the_installer_p95_is_whole_milliseconds_up_to_an_hour(beacon: object) -> None:
    with pytest.raises(ValueError, match="beacon_latency_ms_p95"):
        latency_report(beacon_latency_ms_p95=beacon, upload=[], exposure=[], served=[])  # type: ignore[arg-type]


def test_an_exposure_sample_is_an_interval_of_the_browser_clock() -> None:
    sample = ExposureSample(
        sample_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        pass_id=uuid.uuid4(),
        fetched_at=T0,
        displayed_at=T0 + timedelta(milliseconds=90),
        recorded_by=uuid.uuid4(),
        recorded_at=T0 - timedelta(days=2),  # reloj de la plataforma: nunca entra en la resta
    )
    start, end = sample.interval()
    assert (start.clock, end.clock) == (BROWSER, BROWSER)
    tranche = measure([sample.interval()], BROWSER)
    assert tranche is not None and tranche.median_ms == 90


_durations = st.lists(st.integers(0, 5_000), min_size=0, max_size=30)
_offsets = st.integers(-(10**9), 10**9)


def _report(
    upload: list[int], shown: list[int], served: list[int], platform_ms: int, browser_ms: int
) -> LatencyReport:
    platform_base = T0 + timedelta(milliseconds=platform_ms)
    browser_base = T0 + timedelta(milliseconds=browser_ms)
    return latency_report(
        beacon_latency_ms_p95=200,
        upload=_intervals(PLATFORM, upload, platform_base),
        exposure=_intervals(BROWSER, shown, browser_base),
        served=_intervals(PLATFORM, served, platform_base),
    )


@given(
    upload=_durations,
    shown=_durations,
    served=_durations,
    platform_ms=_offsets,
    browser_ms=_offsets,
)
def test_shifting_any_clock_by_any_amount_changes_no_tranche(
    upload: list[int], shown: list[int], served: list[int], platform_ms: int, browser_ms: int
) -> None:
    """H-12: cada tramo es una resta dentro de su propio reloj; desplazar el reloj del navegador o
    el de la plataforma (el del nodo ni siquiera entra: el tramo 1 lo trae el instalador) no
    cambia ninguna cifra."""
    reference = _report(upload, shown, served, 0, 0)
    shifted = _report(upload, shown, served, platform_ms, browser_ms)
    assert shifted == reference
    repetitions = None if reference.platform is None else reference.platform.repetitions
    assert repetitions == (len(upload) or None)
    assert (reference.exposure is None) == (not shown)
    assert (reference.served is None) == (not served)
