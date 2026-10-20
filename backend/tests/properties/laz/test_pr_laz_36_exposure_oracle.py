"""PR-LAZ-36 (oráculo): el indicador de exposición coincide con la suma directa de los episodios
confirmados de ``coexistence`` y ``dwell``, ``tier_1``, restringida a lo observable; los minutos
pendientes se declaran aparte (TASK-322; BR-LAZ-17, 118 y 119; DE §3.5).

El oráculo (``strategies.exposure.brute_force``) recorre el tiempo **segundo a segundo** con
conjuntos de enteros, sin aritmética de intervalos: un segundo cuenta si es observable, está en el
periodo y no tiene la compuerta de uso revocada; cada episodio aporta la parte de su condición que
cae con seguridad en segundos contados, ``max(0, C - E)`` acotada a esos segundos (``E``: los
segundos de su intervalo que no cuentan). Sobre los episodios generados (todas las familias, los
dos tiers, las cuatro clasificaciones posibles y la ausencia de clasificación, dentro y a caballo
del periodo):

- los minutos **confirmados** son exactamente los del oráculo, y los **pendientes** (sin ninguna
  clasificación), también, **aparte**: quitar todos los pendientes no cambia los confirmados;
- ningún episodio aporta más que su condición ni más que sus instantes contados;
- la distribución horaria suma exactamente los confirmados (nunca los pendientes).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from zoneinfo import ZoneInfo

from hypothesis import given

from tests.properties.laz.strategies.exposure import (
    ExposureCase,
    GeneratedEpisode,
    at,
    brute_force,
    exposure_cases,
)
from vigia_platform.accreditation.exposure.compute import (
    ExposureFigures,
    compute_exposure,
    counted_spans,
    episode_counted_ms,
    to_ms,
)
from vigia_platform.loop.domain.enums import ExposureStatus

_BOGOTA = ZoneInfo("America/Bogota")


def figures(case: ExposureCase, *, hard_signal: bool = True) -> ExposureFigures:
    return compute_exposure(
        period=case.period_span,
        episodes=case.inputs(),
        observable=case.observable_spans(),
        revoked=case.revoked_spans(),
        hard_signal=hard_signal,
        threshold_percent=case.threshold,
        timezone=_BOGOTA,
        single_occupancy=False,
        window_minutes=60,
    )


@given(exposure_cases())
def test_pr_laz_36_confirmed_and_pending_minutes_are_the_direct_sum(case: ExposureCase) -> None:
    result = figures(case)
    oracle = brute_force(case)
    assert result.confirmed_ms == oracle.confirmed_ms
    assert result.pending_ms == oracle.pending_ms
    assert result.observable_ms == oracle.observable_ms
    assert result.confirmed_minutes == oracle.confirmed_ms / 60_000
    assert result.pending_classification_minutes == oracle.pending_ms / 60_000
    assert result.observable_coverage_percent == oracle.observable_ms * 100 / oracle.period_ms
    assert sum(bucket.confirmed_ms for bucket in result.hourly) == oracle.confirmed_ms


@given(exposure_cases())
def test_pr_laz_36_pending_minutes_are_declared_apart_never_added(case: ExposureCase) -> None:
    without_pending = case.with_episodes(
        tuple(episode for episode in case.episodes if episode.outcome is not None)
    )
    assert figures(without_pending).confirmed_ms == figures(case).confirmed_ms
    assert figures(without_pending).pending_ms == 0


@given(exposure_cases())
def test_pr_laz_36_no_episode_contributes_more_than_its_condition_or_its_counted_time(
    case: ExposureCase,
) -> None:
    counted = counted_spans(case.observable_spans(), case.revoked_spans(), case.period_span)
    for episode in case.episodes:
        amount = episode_counted_ms(episode.input(), counted)
        low, high = to_ms(at(episode.start)), to_ms(at(episode.end))
        counted_inside = sum(max(0, min(high, end) - max(low, start)) for start, end in counted)
        assert 0 <= amount <= episode.condition_ms
        assert amount <= counted_inside


def _episode(start: int, end: int, condition_ms: int, outcome: str | None) -> GeneratedEpisode:
    return GeneratedEpisode(
        start=start,
        end=end,
        condition_ms=condition_ms,
        family="dwell",
        tier="tier_1",
        outcome=outcome,
        episode_id=uuid.uuid5(uuid.NAMESPACE_URL, f"vigia/exposure/{start}/{end}/{outcome}"),
    )


def test_a_fully_observed_episode_counts_its_whole_condition() -> None:
    case = ExposureCase(
        period=(0, 3600),
        observable=((0, 3600),),
        revoked=(),
        episodes=(_episode(60, 360, 240_000, "confirmed"), _episode(400, 460, 30_000, None)),
        threshold=80,
    )
    result = figures(case)
    assert (result.confirmed_ms, result.pending_ms) == (240_000, 30_000)
    assert result.status is ExposureStatus.COMPUTED
    assert result.confirmed_minutes == 4.0


def test_a_gap_inside_the_episode_is_taken_out_of_the_condition_in_the_worst_case() -> None:
    # 300 s de episodio, 240 s de condición y 100 s sin observar: lo seguro son 140 s.
    case = ExposureCase(
        period=(0, 3600),
        observable=((0, 100), (200, 3600)),
        revoked=(),
        episodes=(_episode(0, 300, 240_000, "confirmed"),),
        threshold=0,
    )
    assert figures(case).confirmed_ms == 140_000


def test_an_episode_across_the_period_border_is_never_counted_twice() -> None:
    episode = _episode(3500, 3700, 200_000, "confirmed")
    first = ExposureCase((0, 3600), ((0, 7200),), (), (episode,), 0)
    second = ExposureCase((3600, 7200), ((0, 7200),), (), (episode,), 0)
    assert figures(first).confirmed_ms + figures(second).confirmed_ms == 200_000
    shorter = _episode(3500, 3700, 50_000, "confirmed")
    total = (
        figures(first.with_episodes((shorter,))).confirmed_ms
        + figures(second.with_episodes((shorter,))).confirmed_ms
    )
    assert total <= 50_000


def test_other_families_tier_2_and_discarded_outcomes_never_count() -> None:
    counted = _episode(0, 60, 60_000, "confirmed")
    episodes = (
        replace(counted, family="guard_bypass"),
        replace(counted, family="startup_transition"),
        replace(counted, tier="tier_2"),
        replace(counted, outcome="authorized_operation"),
        replace(counted, outcome="false_positive"),
        replace(counted, tier="tier_2", outcome=None),
    )
    result = figures(ExposureCase((0, 3600), ((0, 3600),), (), episodes, 0))
    assert (result.confirmed_ms, result.pending_ms) == (0, 0)
    assert figures(ExposureCase((0, 3600), ((0, 3600),), (), (counted,), 0)).confirmed_ms == 60_000
