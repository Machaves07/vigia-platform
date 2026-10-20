"""Generadores del indicador de exposición (TASK-322; PR-LAZ-36 a 38): ``episodes_with_segments``,
``coverage_inputs`` y el oráculo de fuerza bruta.

Todo se genera sobre una cuadrícula de **segundos** a partir de ``ORIGIN`` (medianoche de Bogotá):
los extremos de periodo, cobertura, revocaciones y episodios son segundos enteros, y la duración de
la condición, cualquier milisegundo hasta la duración del episodio. Así el oráculo puede recorrer
el tiempo **segundo a segundo** (``brute_force``) con conjuntos de enteros, sin ninguna aritmética
de intervalos: es independiente de ``accreditation.exposure.compute``.

Solo datos generados.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from hypothesis import strategies as st

from vigia_platform.accreditation.exposure.compute import EpisodeInput, Span

__all__ = [
    "FAMILIES",
    "ORIGIN",
    "OUTCOMES",
    "ExposureCase",
    "GeneratedEpisode",
    "Oracle",
    "at",
    "brute_force",
    "coverage_inputs",
    "episodes_with_segments",
    "exposure_cases",
    "spans",
]

ORIGIN: Final = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
"""2026-09-01T00:00 en Bogotá."""
UNIT_MS: Final = 1000
FAMILIES: Final = ("coexistence", "dwell", "guard_bypass", "startup_transition")
OUTCOMES: Final = (None, "confirmed", "authorized_operation", "false_positive")
_COUNTED_FAMILIES: Final = frozenset({"coexistence", "dwell"})
_HORIZON: Final = 400
"""Segundos de la cuadrícula: episodios y cobertura caen en ``[0, 400)``."""


def at(second: int) -> datetime:
    return ORIGIN + timedelta(seconds=second)


@dataclass(frozen=True)
class GeneratedEpisode:
    start: int
    end: int
    condition_ms: int
    family: str
    tier: str
    outcome: str | None
    episode_id: uuid.UUID

    def input(self) -> EpisodeInput:
        return EpisodeInput(
            episode_id=self.episode_id,
            family=self.family,
            tier=self.tier,
            started_at=at(self.start),
            ended_at=at(self.end),
            condition_duration_ms=self.condition_ms,
            classification_outcome=self.outcome,
        )


@dataclass(frozen=True)
class ExposureCase:
    period: tuple[int, int]
    observable: tuple[tuple[int, int], ...]
    revoked: tuple[tuple[int, int], ...]
    episodes: tuple[GeneratedEpisode, ...]
    threshold: int

    @property
    def period_span(self) -> Span:
        return Span(at(self.period[0]), at(self.period[1]))

    def observable_spans(self) -> tuple[Span, ...]:
        return spans(self.observable)

    def revoked_spans(self) -> tuple[Span, ...]:
        return spans(self.revoked)

    def inputs(self) -> tuple[EpisodeInput, ...]:
        return tuple(episode.input() for episode in self.episodes)

    def with_episodes(self, episodes: tuple[GeneratedEpisode, ...]) -> ExposureCase:
        return replace(self, episodes=episodes)


def spans(pairs: tuple[tuple[int, int], ...]) -> tuple[Span, ...]:
    return tuple(Span(at(start), at(end)) for start, end in pairs)


def _pair(draw: st.DrawFn, low: int, high: int) -> tuple[int, int]:
    start = draw(st.integers(low, high - 1))
    end = draw(st.integers(start, min(high, start + 200)))
    return start, end


@st.composite
def coverage_inputs(draw: st.DrawFn) -> tuple[tuple[tuple[int, int], ...], ...]:
    """``(observable, revoked)``: intervalos sueltos (pueden solaparse, tocarse o estar vacíos);
    a veces la cobertura es completa, para que el umbral se cumpla."""
    observable = tuple(_pair(draw, 0, _HORIZON) for _ in range(draw(st.integers(0, 5))))
    if draw(st.booleans()):
        observable = ((0, _HORIZON), *observable)
    revoked = tuple(_pair(draw, 0, _HORIZON) for _ in range(draw(st.integers(0, 3))))
    return observable, revoked


@st.composite
def episodes_with_segments(draw: st.DrawFn) -> tuple[GeneratedEpisode, ...]:
    """Episodios de cualquier familia, tier y clasificación, dentro o a caballo del periodo.

    Con sesgo hacia los que cuentan (``coexistence``/``dwell``, ``tier_1``, confirmados o
    pendientes), para que la suma rara vez sea trivial; la condición puede ser la duración entera,
    cualquier parte de ella o, en un episodio anómalo (tramos solapados), **mayor** que su
    intervalo: el indicador nunca cuenta más que sus instantes contados."""
    episodes: list[GeneratedEpisode] = []
    for _ in range(draw(st.integers(0, 6))):
        start, end = _pair(draw, 0, _HORIZON)
        span_ms = (end - start) * UNIT_MS
        condition = draw(
            st.one_of(
                st.just(span_ms),
                st.just(0),
                st.integers(0, span_ms),
                st.integers(0, 2 * span_ms),
            )
        )
        episodes.append(
            GeneratedEpisode(
                start=start,
                end=end,
                condition_ms=condition,
                family=draw(
                    st.one_of(st.sampled_from(("coexistence", "dwell")), st.sampled_from(FAMILIES))
                ),
                tier=draw(st.one_of(st.just("tier_1"), st.sampled_from(("tier_1", "tier_2")))),
                outcome=draw(
                    st.one_of(st.sampled_from(("confirmed", None)), st.sampled_from(OUTCOMES))
                ),
                episode_id=uuid.UUID(int=draw(st.integers(0, 2**64))),
            )
        )
    return tuple(episodes)


@st.composite
def exposure_cases(draw: st.DrawFn) -> ExposureCase:
    period = (0, _HORIZON) if draw(st.booleans()) else _pair(draw, 0, _HORIZON)
    if period[1] == period[0]:
        period = (period[0], period[0] + 1)
    observable, revoked = draw(coverage_inputs())
    return ExposureCase(
        period=period,
        observable=observable,
        revoked=revoked,
        episodes=draw(episodes_with_segments()),
        threshold=draw(st.one_of(st.sampled_from((0, 80, 100)), st.integers(0, 100))),
    )


@dataclass(frozen=True)
class Oracle:
    confirmed_ms: int
    pending_ms: int
    observable_ms: int
    period_ms: int
    contributions: tuple[tuple[GeneratedEpisode, int], ...]
    """Cada episodio confirmado que cuenta, con lo que aporta."""


def _inside(second: int, pairs: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= second < end for start, end in pairs)


def brute_force(case: ExposureCase) -> Oracle:
    """Suma directa, segundo a segundo, de lo que el indicador debe contar."""
    low, high = case.period
    observable = {s for s in range(low, high) if _inside(s, case.observable)}
    counted = {s for s in observable if not _inside(s, case.revoked)}
    confirmed = pending = 0
    contributions: list[tuple[GeneratedEpisode, int]] = []
    for episode in case.episodes:
        if episode.family not in _COUNTED_FAMILIES or episode.tier != "tier_1":
            continue
        seconds = range(episode.start, episode.end)
        inside = sum(1 for s in seconds if s in counted) * UNIT_MS
        outside = len(seconds) * UNIT_MS - inside
        amount = min(inside, max(0, episode.condition_ms - outside))
        if episode.outcome == "confirmed":
            confirmed += amount
            contributions.append((episode, amount))
        elif episode.outcome is None:
            pending += amount
    return Oracle(
        confirmed_ms=confirmed,
        pending_ms=pending,
        observable_ms=len(observable) * UNIT_MS,
        period_ms=(high - low) * UNIT_MS,
        contributions=tuple(contributions),
    )
