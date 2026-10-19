"""Muestra auditada estratificada del paquete mensual (PR-LAZ-30; BR-LAZ-105; TASK-343).

BR-LAZ-105 (PR-LAZ-30): «la muestra toma el 10 % de las reclasificaciones del periodo, con
mínimo 5 y máximo 30 `[estimación propia]`, estratificada por familia y por categoría de motivo,
con semilla registrada, para que la misma semilla reproduzca siempre la misma selección. Si la
población es menor que el mínimo, se toma toda».

Módulo puro: sin E/S, sin acceso a la base ni a FastAPI, y sin leer la hora del sistema.

- ``_allocate`` reparte ``sample_size`` entre estratos de forma **determinista**: solo depende de
  los tamaños de cada estrato y del tamaño de la muestra (método del resto mayor). No hay
  aleatoriedad en el reparto, solo en qué elementos se extraen dentro de cada estrato.
- ``draw_sample`` consume un único ``random.Random(seed)`` en un orden fijo: los estratos se
  recorren ordenados por ``(family, reason_category)`` y, dentro de cada uno, los candidatos se
  ordenan por ``classification_id`` antes de ``rng.sample(...)``. Así la misma semilla siempre
  selecciona lo mismo, y una semilla distinta selecciona (con alta probabilidad) otra cosa.

``family`` y ``reason_category`` viajan como ``str`` (no como los tipos cerrados del expediente)
para mantener este módulo desacoplado de ``loop``/``accreditation.record_types``; el llamador
convierte.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_SEED",
    "SampleCandidate",
    "SampleDraw",
    "StratumDraw",
    "clamp",
    "draw_sample",
]

MAX_SEED: Final = 2**64 - 1
"""Tope de ``seed`` (``AuditSampleSummary.seed`` en ``accreditation.record_types``)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleCandidate:
    """Una reclasificación elegible para la muestra."""

    classification_id: uuid.UUID
    subject_id: uuid.UUID
    family: str
    reason_category: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StratumDraw:
    """Lo que se extrajo de un estrato ``(family, reason_category)``."""

    family: str
    reason_category: str
    population: int
    drawn: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleDraw:
    """El resultado reproducible de ``draw_sample``."""

    population_size: int
    sample_size: int
    seed: int
    strata: tuple[StratumDraw, ...]
    """Ordenados por ``(family, reason_category)``."""
    selected: tuple[uuid.UUID, ...]
    """``classification_id`` elegidos, ordenados para que la salida sea determinista."""


def clamp(value: int, minimum: int, maximum: int) -> int:
    """``value`` acotado a ``[minimum, maximum]``."""
    return max(minimum, min(maximum, value))


def _allocate(
    sizes: Sequence[tuple[tuple[str, str], int]], sample_size: int, total: int
) -> dict[tuple[str, str], int]:
    """El reparto proporcional de ``sample_size`` entre estratos (método del resto mayor).

    ``sample_size`` nunca supera ``total`` (lo garantiza el llamador), así que siempre hay
    capacidad suficiente para repartirlo entero sin que ningún estrato reciba más que su propia
    población. El reparto es puramente una función de los tamaños: nada aleatorio.
    """
    keys = [key for key, _ in sizes]
    population = dict(sizes)
    if sample_size <= 0 or total <= 0:
        return {key: 0 for key in keys}
    exact = {key: population[key] * sample_size / total for key in keys}
    base = {key: min(int(exact[key]), population[key]) for key in keys}
    remaining = sample_size - sum(base.values())
    # Orden determinista: mayor parte fraccionaria primero; empate por la clave del estrato.
    order = sorted(keys, key=lambda key: (-(exact[key] - base[key]), key))
    result = dict(base)
    while remaining > 0:
        progressed = False
        for key in order:
            if remaining <= 0:
                break
            if result[key] < population[key]:
                result[key] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break  # pragma: no cover - solo si sample_size > total, que el llamador evita
    return result


def draw_sample(
    population: Sequence[SampleCandidate],
    *,
    sample_percent: int,
    sample_min: int,
    sample_max: int,
    seed: int,
) -> SampleDraw:
    """La muestra estratificada de ``population`` (BR-LAZ-105); reproducible por ``seed``."""
    if not 0 <= seed <= MAX_SEED:
        raise ValueError(f"seed debe ir de 0 a {MAX_SEED}")
    total = len(population)
    if total == 0:
        return SampleDraw(population_size=0, sample_size=0, seed=seed, strata=(), selected=())

    sample_size = clamp(round(sample_percent / 100 * total), sample_min, sample_max)
    # «Si la población es menor que el mínimo, se toma toda»: el acotado a ``total`` cubre tanto
    # ese caso (el mínimo pedía más de lo que hay) como cualquier otro resto imposible de cumplir.
    sample_size = min(sample_size, total)

    groups: dict[tuple[str, str], list[SampleCandidate]] = {}
    for candidate in population:
        groups.setdefault((candidate.family, candidate.reason_category), []).append(candidate)
    stratum_sizes = sorted(
        ((key, len(items)) for key, items in groups.items()), key=lambda item: item[0]
    )
    shares = _allocate(stratum_sizes, sample_size, total)

    rng = random.Random(seed)  # noqa: S311 - selección reproducible por semilla, no criptográfica
    selected: list[uuid.UUID] = []
    strata: list[StratumDraw] = []
    for key, size in stratum_sizes:
        family, reason_category = key
        share = shares[key]
        items = sorted(groups[key], key=lambda item: str(item.classification_id))
        chosen = rng.sample(items, k=share) if share else []
        selected.extend(item.classification_id for item in chosen)
        strata.append(
            StratumDraw(
                family=family, reason_category=reason_category, population=size, drawn=share
            )
        )
    return SampleDraw(
        population_size=total,
        sample_size=sum(shares.values()),
        seed=seed,
        strata=tuple(strata),
        selected=tuple(sorted(selected, key=str)),
    )
