"""Línea de tiempo de cobertura del paquete (PAT-LAZ-REN-04; BR-LAZ-92, 93 y 97; PR-LAZ-27 y 29).

U-02 sirve la línea de tiempo de una zona en periodos de **a lo sumo 31 días**
(``CoveragePort.linea_de_tiempo``, ``period_too_long``). Para un periodo de hasta 366 días:

- ``segments(start, end)`` parte ``[start, end)`` en tramos consecutivos de 31 días como mucho
  (el último, más corto);
- ``zone_timeline`` pide cada tramo, **concatena** y **fusiona** los intervalos adyacentes de igual
  estado, causa y capa (``merge_intervals``, la misma regla que PR-NUC-51): el resultado es
  idéntico al de una consulta única y sigue siendo una **partición exacta**. Se comprueba al
  componer: un hueco o un solape entre intervalos es una línea de tiempo rota y no se entrega;
- el resumen es la suma de los resúmenes de U-02 por tramo (cada milisegundo cuenta una vez).

**Periodo que aún no terminó**: si ``period_to`` es posterior a ``generated_at`` (marca fijada en
el primer intento), a U-02 se le pide solo hasta ``generated_at`` y el resto es un tramo
``not_observable`` con la causa ``period_not_elapsed``: ese tiempo **no se observó** porque no
había pasado, y nunca se presenta como observado. Así la línea de tiempo cubre siempre el periodo
entero y su resumen suma exactamente su duración.

**Fallo cerrado** (BR-LAZ-101): si U-02 dice que la zona no existe o no está en el alcance, o la
línea de tiempo no es una partición, la exportación falla declarada con ``coverage_unavailable``
y no se publica nada. Un fallo transitorio de la base (``TemporarilyUnavailable``) sale tal cual:
el consumidor libera la solicitud y la retoma desde cero.

Las anotaciones del paquete (``timeline_annotations``) declaran sobre la línea de tiempo los
tramos con acta de walk-test no vigente y las revocaciones de la compuerta de uso, recortados al
periodo: informan, no cambian los intervalos de cobertura (BR-LAZ-97).

Solo corre en el proceso de trabajo (nunca en una petición de persona).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.export.composer_port import ExportInputUnavailable
from vigia_platform.accreditation.manifest.model import (
    NOT_ELAPSED_CAUSE,
    CoverageZoneSummary,
    RegressionPeriod,
    RevokedGate,
)
from vigia_platform.ledger.application.coverage import (
    CoverageInputInvalid,
    CoverageTimeline,
    CoverageZoneNotFound,
    PeriodTooLong,
)
from vigia_platform.ledger.domain.coverage import (
    ClockBasis,
    CoverageInterval,
    CoverageLayer,
    CoveragePeriod,
    CoverageState,
)
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_SEGMENT",
    "TimelineCoverage",
    "ZoneTimeline",
    "merge_intervals",
    "segments",
    "timeline_annotations",
    "timeline_json",
    "zone_timeline",
]

MAX_SEGMENT: Final = timedelta(days=31)
"""Lo más que U-02 sirve de una vez (PAT-NUC-REN-04)."""

_MS: Final = timedelta(milliseconds=1)
_UNAVAILABLE: Final = AccreditationDetailCode.COVERAGE_UNAVAILABLE.value


class TimelineCoverage(Protocol):
    """Lo que la línea de tiempo usa de ``CoveragePort`` (U-02)."""

    async def linea_de_tiempo(
        self, context: ScopeContext, zone_id: uuid.UUID, period: CoveragePeriod
    ) -> CoverageTimeline: ...


def segments(
    start: datetime, end: datetime, max_span: timedelta = MAX_SEGMENT
) -> tuple[CoveragePeriod, ...]:
    """``[start, end)`` en tramos consecutivos de ``max_span`` como mucho (vacío si no hay nada)."""
    if max_span <= timedelta(0):
        raise ValueError("max_span debe ser positivo")
    pieces: list[CoveragePeriod] = []
    cursor = start
    while cursor < end:
        upper = min(end, cursor + max_span)
        pieces.append(CoveragePeriod(cursor, upper))
        cursor = upper
    return tuple(pieces)


def _key(interval: CoverageInterval) -> tuple[Any, ...]:
    return (interval.state, interval.causes, interval.layer)


def merge_intervals(intervals: Iterable[CoverageInterval]) -> tuple[CoverageInterval, ...]:
    """Fusiona los intervalos adyacentes de igual estado, causa y capa (los de un corte)."""
    merged: list[CoverageInterval] = []
    for interval in intervals:
        if (
            merged
            and merged[-1].ends_at == interval.starts_at
            and _key(merged[-1]) == _key(interval)
        ):
            previous = merged[-1]
            merged[-1] = CoverageInterval(
                starts_at=previous.starts_at,
                ends_at=interval.ends_at,
                layer=previous.layer,
                state=previous.state,
                causes=previous.causes,
                subject=None,
                source_record_ids=tuple(
                    sorted({*previous.source_record_ids, *interval.source_record_ids}, key=str)
                ),
                clock_basis=previous.clock_basis,
            )
        else:
            merged.append(interval)
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class ZoneTimeline:
    """La línea de tiempo de una zona en el periodo pedido: partición exacta y su resumen."""

    zone_id: uuid.UUID
    period_from: datetime
    period_to: datetime
    intervals: tuple[CoverageInterval, ...]
    """La de U-02 hasta ``generated_at`` y, si el periodo no había terminado, el tramo final
    ``period_not_elapsed``."""
    summary: CoverageZoneSummary


def _is_partition(intervals: Sequence[CoverageInterval], start: datetime, end: datetime) -> bool:
    cursor = start
    for interval in intervals:
        if interval.starts_at != cursor or interval.ends_at <= interval.starts_at:
            return False
        cursor = interval.ends_at
    return cursor == end


async def zone_timeline(
    coverage: TimelineCoverage,
    context: ScopeContext,
    zone_id: uuid.UUID,
    period_from: datetime,
    period_to: datetime,
    generated_at: datetime,
) -> ZoneTimeline:
    """La línea de tiempo de ``zone_id`` en ``[period_from, period_to)`` por tramos de 31 días.

    Una llamada a U-02 por tramo (a lo sumo 12 para 366 días); ninguna por elemento."""
    observed_until = max(period_from, min(period_to, generated_at))
    pieces: list[CoverageInterval] = []
    totals = dict.fromkeys(
        (
            "observable_ms",
            "degraded_ms",
            "not_observable_ms",
            "no_communication_ms",
            "never_reported_ms",
            "zone_not_active_ms",
        ),
        0,
    )
    for piece in segments(period_from, observed_until):
        try:
            timeline = await coverage.linea_de_tiempo(context, zone_id, piece)
        except (CoverageZoneNotFound, CoverageInputInvalid, PeriodTooLong):
            raise ExportInputUnavailable(_UNAVAILABLE) from None
        if timeline.zone_id != zone_id or not _is_partition(
            timeline.intervals, piece.start, piece.end
        ):
            raise ExportInputUnavailable(_UNAVAILABLE)
        pieces.extend(timeline.intervals)
        for name in totals:
            totals[name] += getattr(timeline.summary, name)
    intervals = list(merge_intervals(pieces))
    not_elapsed_ms = (period_to - observed_until) // _MS
    if observed_until < period_to:
        intervals.append(
            CoverageInterval(
                starts_at=observed_until,
                ends_at=period_to,
                layer=CoverageLayer.PLATFORM_COMMUNICATION,
                state=CoverageState.NOT_OBSERVABLE,
                causes=(NOT_ELAPSED_CAUSE,),
                subject=None,
                source_record_ids=(),
                clock_basis=ClockBasis.PLATFORM,
            )
        )
    if not _is_partition(intervals, period_from, period_to):
        raise ExportInputUnavailable(_UNAVAILABLE)
    summary = CoverageZoneSummary(
        zone_id=zone_id,
        not_elapsed_ms=not_elapsed_ms,
        interval_count=len(intervals),
        **totals,
    )
    if summary.total_ms != (period_to - period_from) // _MS:
        # Cada milisegundo cuenta una vez: un resumen que no suma el periodo esconde un tramo.
        raise ExportInputUnavailable(_UNAVAILABLE)
    return ZoneTimeline(zone_id, period_from, period_to, tuple(intervals), summary)


def timeline_annotations(
    zone_id: uuid.UUID,
    period_from: datetime,
    period_to: datetime,
    regression: Iterable[RegressionPeriod],
    revoked: Iterable[RevokedGate],
) -> list[dict[str, Any]]:
    """Los tramos de acta no vigente y de compuerta de uso revocada de la zona, recortados al
    periodo y ordenados (BR-LAZ-96 y 97)."""
    found: list[tuple[datetime, datetime, str, str | None]] = []
    for item in regression:
        if item.zone_id == zone_id:
            found.append(
                (item.starts_at, item.ends_at or period_to, "walk_test_not_current", item.cause)
            )
    for gate in revoked:
        if gate.zone_id == zone_id:
            found.append((gate.since, gate.until or period_to, "usage_gate_revoked", None))
    result: list[dict[str, Any]] = []
    for start, end, kind, cause in sorted(found, key=lambda item: (item[0], item[2], item[1])):
        start, end = max(start, period_from), min(end, period_to)
        if start < end:
            result.append(
                {
                    "kind": kind,
                    "from": format_timestamp(start),
                    "to": format_timestamp(end),
                    "cause": cause,
                }
            )
    return result


def _interval_json(interval: CoverageInterval) -> dict[str, Any]:
    return {
        "from": format_timestamp(interval.starts_at),
        "to": format_timestamp(interval.ends_at),
        "state": interval.state.value,
        "causes": list(interval.causes),
        "layer": interval.layer.value,
        "duration_ms": interval.duration_ms,
        "source_record_ids": [str(value) for value in interval.source_record_ids],
    }


def timeline_json(timeline: ZoneTimeline, notes: list[dict[str, Any]]) -> dict[str, Any]:
    """El artefacto ``coverage`` de una zona."""
    summary = timeline.summary
    return {
        "zone_id": str(timeline.zone_id),
        "period": {
            "from": format_timestamp(timeline.period_from),
            "to": format_timestamp(timeline.period_to),
        },
        "intervals": [_interval_json(interval) for interval in timeline.intervals],
        "summary": {
            "observable_ms": summary.observable_ms,
            "degraded_ms": summary.degraded_ms,
            "not_observable_ms": summary.not_observable_ms,
            "no_communication_ms": summary.no_communication_ms,
            "never_reported_ms": summary.never_reported_ms,
            "zone_not_active_ms": summary.zone_not_active_ms,
            "not_elapsed_ms": summary.not_elapsed_ms,
            "total_ms": summary.total_ms,
        },
        "annotations": notes,
    }
