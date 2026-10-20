"""Cálculo puro del indicador de exposición de una zona y un mes (TASK-322; BR-LAZ-17 y 118 a
122; PR-LAZ-36 a 38).

Módulo **puro**: sin FastAPI, sin SQLAlchemy, sin E/S y sin hora del sistema. ``service`` le pasa
lo que leyó (episodios de la proyección, línea de tiempo de cobertura de U-02, historia de la
compuerta de uso de U-03 y la marca unipersonal del catálogo) y guarda lo que devuelve.

**Qué se suma** (BR-LAZ-118 y 17): episodios, nunca tramos, de las familias ``coexistence`` y
``dwell`` con ``tier_1``, por su ``total_condition_duration_ms``. Los **confirmados** (última
clasificación ``confirmed``) dan los minutos confirmados; los que **no tienen ninguna
clasificación** dan los minutos pendientes, que se declaran aparte y nunca se suman a los
confirmados (BR-LAZ-119). Los clasificados como operación autorizada o falso positivo no cuentan.

**Instantes contados**: los ``observable`` de la línea de tiempo dentro del periodo, menos los de
compuerta de uso revocada (respuesta 12: con la compuerta revocada el indicador deja de acumular).
``degraded`` no cuenta: solo el estado Observable (BR-LAZ-118).

**Restricción a lo observable** (``episode_counted_ms``): un episodio declara cuánto duró su
condición, no en qué instantes de ``[started_at, ended_at)`` ocurrió. Se cuenta la **cota
inferior** de la condición que cae con seguridad en instantes contados: ``max(0, C - E)``, donde
``C`` es la duración de la condición y ``E`` el tiempo del intervalo del episodio que no se cuenta
(no observable, revocado o fuera del periodo), y nunca más que los instantes contados del
intervalo. Es la cifra que la evidencia respalda sin suponer nada (P6): un episodio entero en
instantes contados suma su condición completa; uno con algún hueco suma menos, nunca más. De ahí
la monotonía de PR-LAZ-37: añadir un intervalo no observable o una revocación solo hace crecer
``E`` y menguar los instantes contados. Un episodio que cruza el borde del mes reparte su condición
sin contarla dos veces.

**Estado** (BR-LAZ-120): sin señal dura declarada, ``not_computable`` y ninguna cifra (nunca cero,
BR-LAZ-115); con la cobertura observable del periodo bajo ``exposure_coverage_threshold_percent``,
``partial`` (la comparación es entera: ``observable·100 < umbral·periodo``); si no, ``computed``.
El periodo es el mes **entero**: lo que aún no transcurrió es tiempo no observado, así que el mes
en curso es ``partial`` hasta que su cobertura alcance el umbral (nunca un valor parcial presentado
como completo).

**Distribución horaria** (BR-LAZ-122, PAT-LAZ-MAN-05): los minutos confirmados por la hora del día
en que empezó cada episodio, en la hora local de la planta. Cubos de 60 minutos; en una zona
unipersonal, del ancho **exacto** de su ventana de agregación (15 a 480), contados desde la
medianoche local con ``loop.domain.time_aggregation.aggregate``, la única función de agregación de
U-04. Si la ventana no divide el día, el último cubo conserva el ancho y termina después de las
24:00 (ningún instante cae en esa cola). Ningún cubo lleva nombre: solo su minuto de inicio y su
ancho, nunca un turno, un equipo ni un grupo.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from vigia_platform.loop.domain.enums import ClassificationOutcome, ExposureStatus
from vigia_platform.loop.domain.time_aggregation import GRID_ORIGIN, aggregate

__all__ = [
    "COUNTED_FAMILIES",
    "COUNTED_TIER",
    "DEFAULT_BUCKET_MINUTES",
    "MINUTES_PER_DAY",
    "EpisodeInput",
    "ExposureFigures",
    "HourlyBucket",
    "Span",
    "bucket_width",
    "compute_exposure",
    "counted_spans",
    "episode_counted_ms",
    "hourly_distribution",
    "measure_ms",
    "to_ms",
]

COUNTED_FAMILIES: Final = frozenset({"coexistence", "dwell"})
"""Familias que suma el indicador (BR-LAZ-118)."""
COUNTED_TIER: Final = "tier_1"
"""Solo tier 1: exige señal dura válida (RF-BOR-03)."""
DEFAULT_BUCKET_MINUTES: Final = 60
"""Cubo de una zona que no es unipersonal: una hora."""
MINUTES_PER_DAY: Final = 24 * 60

_MS: Final = timedelta(milliseconds=1)
_MINUTE_MS: Final = 60_000


def to_ms(moment: datetime) -> int:
    """Milisegundos desde 1970-01-01T00:00Z (la marca tiene que llevar zona horaria)."""
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria")
    return (moment.astimezone(UTC) - GRID_ORIGIN) // _MS


@dataclass(frozen=True, slots=True)
class Span:
    """Intervalo semiabierto ``[start, end)`` con zona horaria."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if to_ms(self.end) < to_ms(self.start):
            raise ValueError("el intervalo termina antes de empezar")


@dataclass(frozen=True, slots=True, kw_only=True)
class EpisodeInput:
    """Un episodio de la proyección con la última clasificación de su hallazgo."""

    episode_id: uuid.UUID
    family: str
    tier: str
    started_at: datetime
    ended_at: datetime
    condition_duration_ms: int
    classification_outcome: str | None
    """``None``: sin clasificar todavía (pendiente)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class HourlyBucket:
    """Un cubo de la distribución horaria: su inicio (minuto del día local) y su ancho."""

    start_minute: int
    width_minutes: int
    confirmed_ms: int

    @property
    def confirmed_minutes(self) -> float:
        return self.confirmed_ms / _MINUTE_MS


@dataclass(frozen=True, slots=True, kw_only=True)
class ExposureFigures:
    """Las tres cifras, el estado y la distribución horaria de una zona y un periodo."""

    status: ExposureStatus
    period_ms: int
    threshold_percent: int
    confirmed_ms: int | None
    pending_ms: int | None
    observable_ms: int | None
    hourly: tuple[HourlyBucket, ...]

    @property
    def confirmed_minutes(self) -> float | None:
        return None if self.confirmed_ms is None else self.confirmed_ms / _MINUTE_MS

    @property
    def pending_classification_minutes(self) -> float | None:
        return None if self.pending_ms is None else self.pending_ms / _MINUTE_MS

    @property
    def observable_coverage_percent(self) -> float | None:
        if self.observable_ms is None:
            return None
        return self.observable_ms * 100 / self.period_ms


# --- intervalos ------------------------------------------------------------------------------


def _normalized(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Ordenados, sin vacíos y fusionados los que se tocan o se solapan."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(span for span in spans if span[1] > span[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(
    base: Sequence[tuple[int, int]], removed: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in base:
        cursor = start
        for cut_start, cut_end in removed:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
        if cursor < end:
            result.append((cursor, end))
    return result


def _ms_spans(spans: Iterable[Span]) -> list[tuple[int, int]]:
    return [(to_ms(span.start), to_ms(span.end)) for span in spans]


def counted_spans(
    observable: Iterable[Span], revoked: Iterable[Span], period: Span
) -> tuple[tuple[int, int], ...]:
    """Los instantes contados en milisegundos: observable ∩ periodo, menos lo revocado."""
    low, high = to_ms(period.start), to_ms(period.end)
    clipped = ((max(low, start), min(high, end)) for start, end in _ms_spans(observable))
    return tuple(_subtract(_normalized(clipped), _normalized(_ms_spans(revoked))))


def measure_ms(spans: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in spans)


def _overlap_ms(low: int, high: int, counted: Sequence[tuple[int, int]]) -> int:
    return sum(
        max(0, min(high, end) - max(low, start))
        for start, end in counted
        if start < high and end > low
    )


def episode_counted_ms(episode: EpisodeInput, counted: Sequence[tuple[int, int]]) -> int:
    """La condición del episodio que cae con seguridad en instantes contados (cota inferior)."""
    low, high = to_ms(episode.started_at), to_ms(episode.ended_at)
    if high <= low or episode.condition_duration_ms <= 0:
        return 0
    inside = _overlap_ms(low, high, counted)
    outside = (high - low) - inside
    return min(inside, max(0, episode.condition_duration_ms - outside))


# --- distribución horaria --------------------------------------------------------------------


def bucket_width(*, single_occupancy: bool, window_minutes: int) -> int:
    """Ancho de los cubos: la ventana de agregación en una zona unipersonal; si no, una hora."""
    return window_minutes if single_occupancy else DEFAULT_BUCKET_MINUTES


def _bucket_start_minute(moment: datetime, timezone: ZoneInfo, width: int) -> int:
    """Minuto del día local en que empieza el cubo de ``moment`` (``aggregate`` sobre el reloj
    de pared, de modo que un día de cambio de hora no corre los cubos)."""
    local = moment.astimezone(timezone)
    wall = GRID_ORIGIN + timedelta(
        hours=local.hour, minutes=local.minute, seconds=local.second, microseconds=local.microsecond
    )
    return (aggregate(wall, GRID_ORIGIN, width) - GRID_ORIGIN) // timedelta(minutes=1)


def hourly_distribution(
    contributions: Iterable[tuple[datetime, int]],
    *,
    timezone: ZoneInfo,
    single_occupancy: bool,
    window_minutes: int,
) -> tuple[HourlyBucket, ...]:
    """Los cubos del día con los milisegundos confirmados que empezaron en cada uno."""
    width = bucket_width(single_occupancy=single_occupancy, window_minutes=window_minutes)
    count = -(-MINUTES_PER_DAY // width)
    totals = [0] * count
    for started_at, counted_ms in contributions:
        if counted_ms > 0:
            totals[_bucket_start_minute(started_at, timezone, width) // width] += counted_ms
    return tuple(
        HourlyBucket(start_minute=index * width, width_minutes=width, confirmed_ms=total)
        for index, total in enumerate(totals)
    )


# --- el indicador ----------------------------------------------------------------------------


def _counts(episode: EpisodeInput) -> bool:
    return episode.family in COUNTED_FAMILIES and episode.tier == COUNTED_TIER


def compute_exposure(
    *,
    period: Span,
    episodes: Iterable[EpisodeInput],
    observable: Iterable[Span],
    revoked: Iterable[Span],
    hard_signal: bool,
    threshold_percent: int,
    timezone: ZoneInfo,
    single_occupancy: bool,
    window_minutes: int,
) -> ExposureFigures:
    """El indicador de ``period``: ``not_computable`` sin señal dura, ``partial`` bajo el umbral."""
    if type(threshold_percent) is not int or not 0 <= threshold_percent <= 100:
        raise ValueError("el umbral de cobertura es un entero de 0 a 100")
    period_ms = to_ms(period.end) - to_ms(period.start)
    if period_ms <= 0:
        raise ValueError("el periodo no puede estar vacío")
    if not hard_signal:
        return ExposureFigures(
            status=ExposureStatus.NOT_COMPUTABLE,
            period_ms=period_ms,
            threshold_percent=threshold_percent,
            confirmed_ms=None,
            pending_ms=None,
            observable_ms=None,
            hourly=(),
        )
    observable = tuple(observable)
    counted = counted_spans(observable, revoked, period)
    observable_ms = measure_ms(counted_spans(observable, (), period))
    confirmed: list[tuple[datetime, int]] = []
    pending_ms = 0
    for episode in episodes:
        if not _counts(episode):
            continue
        if episode.classification_outcome == ClassificationOutcome.CONFIRMED.value:
            confirmed.append((episode.started_at, episode_counted_ms(episode, counted)))
        elif episode.classification_outcome is None:
            pending_ms += episode_counted_ms(episode, counted)
    partial = observable_ms * 100 < threshold_percent * period_ms
    return ExposureFigures(
        status=ExposureStatus.PARTIAL if partial else ExposureStatus.COMPUTED,
        period_ms=period_ms,
        threshold_percent=threshold_percent,
        confirmed_ms=sum(counted_ms for _, counted_ms in confirmed),
        pending_ms=pending_ms,
        observable_ms=observable_ms,
        hourly=hourly_distribution(
            confirmed,
            timezone=timezone,
            single_occupancy=single_occupancy,
            window_minutes=window_minutes,
        ),
    )
