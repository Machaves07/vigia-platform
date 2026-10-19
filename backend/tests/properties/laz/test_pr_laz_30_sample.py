"""PR-LAZ-30: muestra auditada estratificada del paquete mensual (BR-LAZ-105; TASK-343).

Para poblaciones y semillas generadas: el tamaño de la muestra sigue la fórmula acotada por la
población; la selección es reproducible con la misma semilla y distinta (con alta probabilidad)
con otra; la proporción por estrato se conserva dentro del error de redondeo del método del resto
mayor; los estratos están ordenados, son exactamente los que aparecen en la población y suman el
tamaño de la muestra; y una semilla fuera de ``[0, MAX_SEED]`` se rechaza.

Módulo puro bajo prueba: ``vigia_platform.accreditation.monthly.sample``. Sin base de datos.
"""

from __future__ import annotations

import math
import uuid

import pytest
from hypothesis import assume, given

from tests.properties.laz.strategies.monthly import (
    FAMILIES,
    REASON_CATEGORIES,
    SampleParameters,
    populations,
    sample_parameters,
)
from vigia_platform.accreditation.monthly.sample import (
    MAX_SEED,
    SampleCandidate,
    SampleDraw,
    draw_sample,
)


def _draw(population: list[SampleCandidate], parameters: SampleParameters) -> SampleDraw:
    return draw_sample(
        population,
        sample_percent=parameters.sample_percent,
        sample_min=parameters.sample_min,
        sample_max=parameters.sample_max,
        seed=parameters.seed,
    )


@given(populations(), sample_parameters())
def test_sample_size_follows_the_bounded_formula(
    population: list[SampleCandidate], parameters: SampleParameters
) -> None:
    draw = _draw(population, parameters)
    total = len(population)
    # Fórmula de BR-LAZ-105 recalculada aquí, no delegada a ``clamp`` del módulo bajo prueba.
    raw = round(parameters.sample_percent / 100 * total)
    bounded = max(parameters.sample_min, min(parameters.sample_max, raw))
    expected = min(bounded, total)
    assert draw.sample_size == expected, (draw.sample_size, expected, parameters, total)
    assert 0 <= draw.sample_size <= draw.population_size
    assert draw.population_size == total == len(population)


@given(populations(), sample_parameters())
def test_population_at_or_below_the_minimum_is_taken_whole(
    population: list[SampleCandidate], parameters: SampleParameters
) -> None:
    assume(len(population) <= parameters.sample_min)
    draw = _draw(population, parameters)
    assert draw.sample_size == len(population)
    assert len(draw.selected) == len(population)


@given(populations(), sample_parameters())
def test_the_same_seed_reproduces_the_same_selection(
    population: list[SampleCandidate], parameters: SampleParameters
) -> None:
    first = _draw(population, parameters)
    second = _draw(population, parameters)
    assert first.selected == second.selected
    assert first.strata == second.strata


def test_different_seed_usually_changes_the_selection() -> None:
    """No es una garantía por par de semillas, sino estadística: 20 semillas distintas no caen
    siempre en la misma selección sobre una población variada (PR-LAZ-30)."""
    population = [
        SampleCandidate(
            classification_id=uuid.UUID(int=i),
            subject_id=uuid.UUID(int=10_000 + i),
            family=FAMILIES[i % len(FAMILIES)],
            reason_category=REASON_CATEGORIES[(i // len(FAMILIES)) % len(REASON_CATEGORIES)],
        )
        for i in range(60)
    ]
    selections = {
        draw_sample(population, sample_percent=50, sample_min=5, sample_max=40, seed=seed).selected
        for seed in range(20)
    }
    assert len(selections) > 1, selections


@given(populations(), sample_parameters())
def test_stratum_drawn_stays_within_one_of_the_exact_proportional_share(
    population: list[SampleCandidate], parameters: SampleParameters
) -> None:
    """``_allocate`` es el método del resto mayor: cada estrato recibe el piso de su parte exacta
    o ese piso más uno (nunca más, nunca menos), y nunca más que su propia población."""
    draw = _draw(population, parameters)
    for stratum in draw.strata:
        assert stratum.drawn <= stratum.population
        if draw.population_size == 0:
            continue
        exact = stratum.population * draw.sample_size / draw.population_size
        assert math.floor(exact) <= stratum.drawn <= math.ceil(exact), (stratum, exact)


@given(populations(), sample_parameters())
def test_strata_are_sorted_and_cover_exactly_the_distinct_pairs_present(
    population: list[SampleCandidate], parameters: SampleParameters
) -> None:
    draw = _draw(population, parameters)
    keys = [(stratum.family, stratum.reason_category) for stratum in draw.strata]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    expected = {(candidate.family, candidate.reason_category) for candidate in population}
    assert set(keys) == expected
    assert sum(stratum.drawn for stratum in draw.strata) == draw.sample_size


def test_a_seed_outside_0_to_max_seed_is_rejected() -> None:
    for bad_seed in (-1, MAX_SEED + 1):
        with pytest.raises(ValueError):
            draw_sample([], sample_percent=10, sample_min=1, sample_max=1, seed=bad_seed)
