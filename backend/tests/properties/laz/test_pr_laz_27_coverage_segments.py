"""PR-LAZ-27: la cobertura por tramos de 31 días es la de una consulta única (TASK-318;
PAT-LAZ-REN-04; BR-LAZ-92; se apoya en PR-NUC-51).

**Propiedad** (``long_periods``: periodos de 32 a 366 días con cambios por todas partes, también
justo en los cortes de 31 días y a un milisegundo de ellos): ``timeline.zone_timeline`` sobre un
``CoveragePort`` que **rechaza** todo periodo de más de 31 días (``compose_timeline`` real, con
``period_too_long``) da exactamente los intervalos y el resumen de la composición de **una
consulta única** del periodo entero (el oráculo: la misma función pura sin el tope), es una
partición exacta del periodo y pide ``ceil(días / 31)`` tramos, ninguno de más de 31 días.

Además, ejemplos de los bordes: exactamente 31 días es un tramo y 31 días y un milisegundo son
dos; un periodo que termina después de ``generated_at`` cierra con el tramo
``period_not_elapsed``; sin cobertura, ``coverage_unavailable``.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import uuid
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from hypothesis import given, settings

from tests.properties.laz.strategies.export import BASE, LongPeriod, long_periods, world_context
from vigia_platform.accreditation.export.composer_port import ExportInputUnavailable
from vigia_platform.accreditation.export.timeline import MAX_SEGMENT, segments, zone_timeline
from vigia_platform.accreditation.manifest.model import NOT_ELAPSED_CAUSE
from vigia_platform.ledger.application.coverage import CoverageTimeline, CoverageZoneNotFound
from vigia_platform.ledger.domain import coverage as coverage_module
from vigia_platform.ledger.domain.coverage import (
    CoverageInputs,
    CoveragePeriod,
    CoverageState,
    compose_timeline,
)
from vigia_platform.shared.context import ScopeContext

ZONE = uuid.UUID(int=0x2701)
ORGANIZATION = uuid.UUID(int=0x2702)
CONTEXT = world_context(ORGANIZATION)


class SegmentedCoverage:
    """``CoveragePort`` de una zona con el tope real de 31 días; anota cada periodo pedido."""

    def __init__(self, inputs: CoverageInputs, *, missing: bool = False) -> None:
        self.inputs = inputs
        self.missing = missing
        self.periods: list[CoveragePeriod] = []

    async def linea_de_tiempo(
        self, context: ScopeContext, zone_id: uuid.UUID, period: CoveragePeriod
    ) -> CoverageTimeline:
        self.periods.append(period)
        if self.missing or zone_id != ZONE:
            raise CoverageZoneNotFound()
        composition = compose_timeline(self.inputs, period)  # period_too_long > 31 días
        return CoverageTimeline(
            organization_id=context.organization_id,
            plant_id=uuid.UUID(int=0x2703),
            zone_id=zone_id,
            period=composition.period,
            intervals=composition.intervals,
            node_intervals=composition.node_intervals,
            summary=composition.summary,
        )


def single_query(inputs: CoverageInputs, period: CoveragePeriod) -> Any:
    """El oráculo: la composición de una consulta única, sin el tope de 31 días."""
    with mock.patch.object(coverage_module, "_MAX_PERIOD_MS", 10**15):
        return compose_timeline(inputs, period)


def _timeline(scenario: LongPeriod, port: SegmentedCoverage, generated_at: Any = None) -> Any:
    period = scenario.period
    return asyncio.run(
        zone_timeline(
            port,
            CONTEXT,
            ZONE,
            period.start,
            period.end,
            generated_at if generated_at is not None else period.end,
        )
    )


@settings(max_examples=150, deadline=None)
@given(scenario=long_periods())
def test_pr_laz_27_segments_compose_to_the_single_query(scenario: LongPeriod) -> None:
    port = SegmentedCoverage(scenario.inputs)
    timeline = _timeline(scenario, port)
    whole = single_query(scenario.inputs, scenario.period)
    assert list(timeline.intervals) == list(whole.intervals)
    summary, expected = timeline.summary, whole.summary
    assert (
        summary.observable_ms,
        summary.degraded_ms,
        summary.not_observable_ms,
        summary.no_communication_ms,
        summary.never_reported_ms,
        summary.zone_not_active_ms,
    ) == (
        expected.observable_ms,
        expected.degraded_ms,
        expected.not_observable_ms,
        expected.no_communication_ms,
        expected.never_reported_ms,
        expected.zone_not_active_ms,
    )
    assert summary.not_elapsed_ms == 0
    # Partición exacta del periodo.
    assert timeline.intervals[0].starts_at == scenario.period.start
    assert timeline.intervals[-1].ends_at == scenario.period.end
    for before, after in itertools.pairwise(timeline.intervals):
        assert before.ends_at == after.starts_at
    assert summary.total_ms == (scenario.period.end - scenario.period.start) // timedelta(
        milliseconds=1
    )
    # Tramos consecutivos de 31 días como mucho, tantos como hacen falta.
    days = (scenario.period.end - scenario.period.start) / timedelta(days=31)
    assert len(port.periods) == math.ceil(days)
    assert all(piece.end - piece.start <= MAX_SEGMENT for piece in port.periods)
    assert port.periods[0].start == scenario.period.start
    assert port.periods[-1].end == scenario.period.end


@pytest.mark.parametrize(
    ("length", "pieces"),
    [
        (timedelta(days=31), 1),
        (timedelta(days=31, milliseconds=1), 2),
        (timedelta(days=62), 2),
        (timedelta(days=366), 12),
        (timedelta(milliseconds=1), 1),
    ],
)
def test_segments_at_the_31_day_border(length: timedelta, pieces: int) -> None:
    cut = segments(BASE, BASE + length)
    assert len(cut) == pieces
    assert cut[0].start == BASE and cut[-1].end == BASE + length
    assert all(piece.end - piece.start <= MAX_SEGMENT for piece in cut)


def test_an_empty_period_has_no_segment() -> None:
    assert segments(BASE, BASE) == ()


@settings(max_examples=60, deadline=None)
@given(scenario=long_periods(min_days=32, max_days=120))
def test_a_period_ending_after_generated_at_closes_with_period_not_elapsed(
    scenario: LongPeriod,
) -> None:
    """Lo que aún no había pasado no se observó: un tramo final ``not_observable`` con su causa
    y nada pedido a U-02 después de ``generated_at``."""
    period = scenario.period
    generated_at = period.start + (period.end - period.start) / 3
    generated_at = generated_at.replace(microsecond=generated_at.microsecond // 1000 * 1000)
    port = SegmentedCoverage(scenario.inputs)
    timeline = _timeline(scenario, port, generated_at)
    tail = timeline.intervals[-1]
    assert (tail.starts_at, tail.ends_at) == (generated_at, period.end)
    assert tail.state is CoverageState.NOT_OBSERVABLE
    assert tail.causes == (NOT_ELAPSED_CAUSE,)
    assert timeline.summary.not_elapsed_ms == (period.end - generated_at) // timedelta(
        milliseconds=1
    )
    assert timeline.summary.total_ms == (period.end - period.start) // timedelta(milliseconds=1)
    assert max(piece.end for piece in port.periods) == generated_at


@settings(max_examples=10, deadline=None)
@given(scenario=long_periods(min_days=32, max_days=70))
def test_without_coverage_the_export_fails_declared(scenario: LongPeriod) -> None:
    with pytest.raises(ExportInputUnavailable) as failure:
        _timeline(scenario, SegmentedCoverage(scenario.inputs, missing=True))
    assert failure.value.failure_code == "accreditation_coverage_unavailable"
