"""PR-LAZ-37 (monotonía): añadir un intervalo no observable o revocar la compuerta de uso nunca
aumenta los minutos confirmados; sin señal dura, ``not_computable``; bajo el umbral de cobertura,
``partial`` y nunca un valor presentado como completo (TASK-322; BR-LAZ-115, 118 y 120;
respuesta 12; P6, P10).

Sobre ``coverage_inputs`` con inserciones (``strategies.exposure``):

- quitar de la cobertura observable un intervalo cualquiera (dentro, fuera o a caballo del
  periodo y de los episodios) no aumenta los confirmados, los pendientes ni la cobertura;
- añadir un intervalo de compuerta revocada no aumenta los confirmados ni los pendientes, y no
  toca la cobertura (la revocación deja de acumular, no esconde lo observado);
- sin señal dura, para **cualquier** entrada: ``not_computable`` sin ninguna cifra (``None``, nunca
  cero) y sin distribución horaria;
- con señal, ``partial`` si y solo si ``observable·100 < umbral·periodo``, con los bordes exactos.
"""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.exposure import (
    ExposureCase,
    GeneratedEpisode,
    at,
    exposure_cases,
)
from vigia_platform.accreditation.exposure.compute import (
    ExposureFigures,
    Span,
    compute_exposure,
)
from vigia_platform.loop.domain.enums import ExposureStatus

_BOGOTA = ZoneInfo("America/Bogota")


def figures(
    case: ExposureCase,
    *,
    observable: tuple[Span, ...] | None = None,
    revoked: tuple[Span, ...] | None = None,
    hard_signal: bool = True,
) -> ExposureFigures:
    return compute_exposure(
        period=case.period_span,
        episodes=case.inputs(),
        observable=case.observable_spans() if observable is None else observable,
        revoked=case.revoked_spans() if revoked is None else revoked,
        hard_signal=hard_signal,
        threshold_percent=case.threshold,
        timezone=_BOGOTA,
        single_occupancy=False,
        window_minutes=60,
    )


def _without(spans: tuple[Span, ...], hole: Span) -> tuple[Span, ...]:
    """``spans`` con ``hole`` convertido en no observable."""
    result: list[Span] = []
    for span in spans:
        if span.end <= hole.start or span.start >= hole.end:
            result.append(span)
            continue
        if span.start < hole.start:
            result.append(Span(span.start, hole.start))
        if hole.end < span.end:
            result.append(Span(hole.end, span.end))
    return tuple(result)


@st.composite
def holes(draw: st.DrawFn) -> Span:
    start = draw(st.integers(-20, 420))
    return Span(at(start), at(draw(st.integers(start, start + 300))))


@given(exposure_cases(), holes())
def test_pr_laz_37_a_new_not_observable_interval_never_increases_the_figures(
    case: ExposureCase, hole: Span
) -> None:
    before = figures(case)
    after = figures(case, observable=_without(case.observable_spans(), hole))
    assert after.confirmed_ms is not None and before.confirmed_ms is not None
    assert after.confirmed_ms <= before.confirmed_ms
    assert after.pending_ms is not None and before.pending_ms is not None
    assert after.pending_ms <= before.pending_ms
    assert after.observable_ms is not None and before.observable_ms is not None
    assert after.observable_ms <= before.observable_ms


@given(exposure_cases(), holes())
def test_pr_laz_37_revoking_the_usage_gate_never_increases_the_figures(
    case: ExposureCase, revocation: Span
) -> None:
    before = figures(case)
    after = figures(case, revoked=(*case.revoked_spans(), revocation))
    assert after.confirmed_ms is not None and before.confirmed_ms is not None
    assert after.confirmed_ms <= before.confirmed_ms
    assert after.pending_ms is not None and before.pending_ms is not None
    assert after.pending_ms <= before.pending_ms
    assert after.observable_ms == before.observable_ms


@given(exposure_cases())
def test_pr_laz_37_without_hard_signal_it_is_always_not_computable_and_never_zero(
    case: ExposureCase,
) -> None:
    result = figures(case, hard_signal=False)
    assert result.status is ExposureStatus.NOT_COMPUTABLE
    assert (result.confirmed_ms, result.pending_ms, result.observable_ms) == (None, None, None)
    assert (
        result.confirmed_minutes,
        result.pending_classification_minutes,
        result.observable_coverage_percent,
    ) == (None, None, None)
    assert result.hourly == ()


@given(exposure_cases())
def test_pr_laz_37_below_the_threshold_it_is_partial_never_presented_as_complete(
    case: ExposureCase,
) -> None:
    result = figures(case)
    assert result.observable_ms is not None
    below = result.observable_ms * 100 < case.threshold * result.period_ms
    assert result.status is (ExposureStatus.PARTIAL if below else ExposureStatus.COMPUTED)


def _confirmed(start: int, end: int) -> GeneratedEpisode:
    return GeneratedEpisode(
        start=start,
        end=end,
        condition_ms=(end - start) * 1000,
        family="coexistence",
        tier="tier_1",
        outcome="confirmed",
        episode_id=uuid.uuid5(uuid.NAMESPACE_URL, f"vigia/exposure/37/{start}/{end}"),
    )


def test_the_threshold_borders_are_exact() -> None:
    # 1000 s de periodo: 800 s observables son el 80 % justo (completo); 799 s, parcial.
    exact = ExposureCase((0, 1000), ((0, 800),), (), (_confirmed(10, 70),), 80)
    assert figures(exact).status is ExposureStatus.COMPUTED
    short = ExposureCase((0, 1000), ((0, 799),), (), (_confirmed(10, 70),), 80)
    assert figures(short).status is ExposureStatus.PARTIAL
    assert figures(short).confirmed_ms == 60_000
    assert figures(ExposureCase((0, 1000), (), (), (), 0)).status is ExposureStatus.COMPUTED
    full = ExposureCase((0, 1000), ((0, 1000),), (), (), 100)
    assert figures(full).status is ExposureStatus.COMPUTED
    almost = ExposureCase((0, 1000), ((0, 999),), (), (), 100)
    assert figures(almost).status is ExposureStatus.PARTIAL


def test_a_revocation_over_the_episode_stops_the_accumulation() -> None:
    case = ExposureCase((0, 1000), ((0, 1000),), (), (_confirmed(100, 200),), 0)
    assert figures(case).confirmed_ms == 100_000
    revoked = figures(case, revoked=(Span(at(150), at(1000)),))
    assert revoked.confirmed_ms == 50_000
    assert figures(case, revoked=(Span(at(0), at(1000)),)).confirmed_ms == 0
