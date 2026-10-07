"""Latencia del acta en cuatro tramos que no se restan (NFR-GOB-70 y su nota D-2; errata U03-H-17).

Cada tramo se mide con **su propio reloj** y se consigna por separado con mediana, percentil 95,
máximo y repeticiones; ningún cálculo resta marcas de relojes distintos (PAT-GOB-REN-08, H-12):

=====================  ===========================================  ==========================
Tramo                  Intervalo                                    Reloj (``measured_by``)
=====================  ===========================================  ==========================
1 (``node``)           hecho → aviso, el p95 que el instalador lee   ``installer``
                       del ``LocalStatus`` del nodo
2 (``platform``)       ``ClipUploadGrant.issued_at`` →               ``platform``
                       ``VerificationClip.received_at``
3a (``exposure``)      ``fetched_at`` → ``displayed_at`` de las      ``browser``
                       muestras de U-05
3b (``served``)        ``received_at`` → ``first_served_at`` del     ``platform``
                       clip
=====================  ===========================================  ==========================

La resta solo existe en ``Stamp.until``, que exige el **mismo** reloj (``ClockMismatch`` si no):
la estructura impide restar una marca del navegador a una de la plataforma. Un tramo sin
intervalos queda **«no medido»** (``None``): nunca se inventa una cifra (P6). Un intervalo
negativo del mismo reloj (un reloj que retrocedió) no es una medida y no cuenta.

**Estadísticos** `[método]`: rango más cercano sobre los milisegundos enteros (mediana = p50,
p95 = el valor de rango ``ceil(0,95·n)``), así que cada cifra es una medida real y nunca una
interpolación. La **suma orientativa** de las medianas medidas (y la de los p95, que el esquema
v1 del acta ya traía) va marcada como tal: nunca se promete como cifra única.

Funciones puras: ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import enum
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_platform.catalog.domain.time_windows import utc_instant

__all__ = [
    "MAX_LATENCY_MS",
    "ClockMismatch",
    "ExposureSample",
    "LatencyReport",
    "MeasuredBy",
    "Stamp",
    "Tranche",
    "TrancheMeasure",
    "TrancheName",
    "latency_report",
    "measure",
    "nearest_rank",
]

MAX_LATENCY_MS: Final = 3_600_000
"""Tope de un intervalo (``LatencyMs`` del registro): más de una hora no es una latencia."""
_MILLISECOND: Final = timedelta(milliseconds=1)


class MeasuredBy(enum.StrEnum):
    """El reloj de un tramo (``LatencyMeasuredBy`` del registro)."""

    INSTALLER = "installer"
    PLATFORM = "platform"
    BROWSER = "browser"
    NODE = "node"


class TrancheName(enum.StrEnum):
    """Los cuatro tramos, con el nombre de su campo en el acta."""

    NODE = "node_tranche"
    PLATFORM = "platform_tranche"
    EXPOSURE = "exposure_tranche"
    SERVED = "served_tranche"


class ClockMismatch(ValueError):
    """Se intentó restar marcas de dos relojes distintos (prohibido, NFR-GOB-70)."""

    def __init__(self, first: MeasuredBy, second: MeasuredBy) -> None:
        super().__init__(f"no se restan marcas de relojes distintos ({first} y {second})")


@dataclass(frozen=True, slots=True)
class Stamp:
    """Una marca con el reloj que la tomó."""

    clock: MeasuredBy
    at: datetime

    def until(self, later: Stamp) -> int:
        """Milisegundos enteros de ``self`` a ``later``, **solo** si son del mismo reloj."""
        if later.clock is not self.clock:
            raise ClockMismatch(self.clock, later.clock)
        return (utc_instant(later.at) - utc_instant(self.at)) // _MILLISECOND


def nearest_rank(ordered: Sequence[int], percentile: int) -> int:
    """El valor de rango ``ceil(p·n/100)`` de ``ordered`` (ordenada, no vacía)."""
    if not ordered or not 0 < percentile <= 100:
        raise ValueError("percentil de 1 a 100 sobre una lista no vacía")
    rank = math.ceil(percentile * len(ordered) / 100)
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class TrancheMeasure:
    """``{median_ms, p95_ms, max_ms, repetitions}`` de un tramo con su reloj."""

    measured_by: MeasuredBy
    p95_ms: int
    median_ms: int | None = None
    max_ms: int | None = None
    repetitions: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "repetitions": self.repetitions,
            "measured_by": MeasuredBy(self.measured_by).value,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TrancheMeasure:
        def optional(key: str) -> int | None:
            item = value.get(key)
            return None if item is None else int(item)

        return cls(
            measured_by=MeasuredBy(value["measured_by"]),
            p95_ms=int(value["p95_ms"]),
            median_ms=optional("median_ms"),
            max_ms=optional("max_ms"),
            repetitions=optional("repetitions"),
        )


Tranche = tuple[Stamp, Stamp]
"""Un intervalo: inicio y fin, cada uno con su reloj."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ExposureSample:
    """Una muestra de exposición (tramo 3a): ``fetched_at`` y ``displayed_at`` del navegador.

    ``recorded_at`` es la hora de la plataforma al recibirla: nunca se resta a las otras dos.
    """

    sample_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    session_id: uuid.UUID
    pass_id: uuid.UUID
    fetched_at: datetime
    displayed_at: datetime
    recorded_by: uuid.UUID
    recorded_at: datetime

    def interval(self) -> Tranche:
        """El intervalo del tramo 3a, con el reloj del navegador en los dos extremos."""
        return Stamp(MeasuredBy.BROWSER, self.fetched_at), Stamp(
            MeasuredBy.BROWSER, self.displayed_at
        )


def measure(intervals: Iterable[Tranche], measured_by: MeasuredBy) -> TrancheMeasure | None:
    """Mediana, p95, máximo y repeticiones de los intervalos de un reloj; ``None`` sin ninguno.

    Cada intervalo se resta con ``Stamp.until`` (mismo reloj o ``ClockMismatch``); los dos extremos
    tienen que ser del reloj del tramo. Un intervalo negativo o mayor que ``MAX_LATENCY_MS`` no
    cuenta.
    """
    values: list[int] = []
    for start, end in intervals:
        if start.clock is not measured_by:
            raise ClockMismatch(measured_by, start.clock)
        elapsed = start.until(end)
        if 0 <= elapsed <= MAX_LATENCY_MS:
            values.append(elapsed)
    if not values:
        return None
    ordered = sorted(values)
    return TrancheMeasure(
        measured_by=measured_by,
        median_ms=nearest_rank(ordered, 50),
        p95_ms=nearest_rank(ordered, 95),
        max_ms=ordered[-1],
        repetitions=len(ordered),
    )


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """Los cuatro tramos por separado y las sumas orientativas (``latency`` del acta)."""

    node: TrancheMeasure | None
    platform: TrancheMeasure | None
    exposure: TrancheMeasure | None
    served: TrancheMeasure | None

    def tranches(self) -> dict[TrancheName, TrancheMeasure | None]:
        return {
            TrancheName.NODE: self.node,
            TrancheName.PLATFORM: self.platform,
            TrancheName.EXPOSURE: self.exposure,
            TrancheName.SERVED: self.served,
        }

    @property
    def not_measured(self) -> tuple[TrancheName, ...]:
        """Los tramos «no medido» (sin una sola muestra)."""
        return tuple(name for name, tranche in self.tranches().items() if tranche is None)

    @property
    def indicative_sum_median_ms(self) -> int | None:
        """Suma **orientativa** de las medianas medidas (el tramo 1 no trae mediana)."""
        medians = [t.median_ms for t in self.tranches().values() if t and t.median_ms is not None]
        return sum(medians) if medians else None

    @property
    def indicative_sum_p95_ms(self) -> int:
        """Suma **orientativa** de los p95 medidos (cero si ningún tramo se midió)."""
        return sum(tranche.p95_ms for tranche in self.tranches().values() if tranche)

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            name.value: None if tranche is None else tranche.to_json()
            for name, tranche in self.tranches().items()
        }
        document.update(
            not_measured=[name.value for name in self.not_measured],
            indicative_sum_median_ms=self.indicative_sum_median_ms,
            indicative_sum_p95_ms=self.indicative_sum_p95_ms,
            indicative=True,
        )
        return document


def latency_report(
    *,
    beacon_latency_ms_p95: int,
    upload: Iterable[Tranche],
    exposure: Iterable[Tranche],
    served: Iterable[Tranche],
) -> LatencyReport:
    """El informe del acta: el p95 del instalador como tramo 1 y los otros tres medidos.

    ``upload`` (tramo 2) y ``served`` (tramo 3b) son intervalos de la plataforma; ``exposure``
    (tramo 3a), del navegador. El tramo 1 solo trae el p95 (sin mediana, máximo ni repeticiones):
    el instalador lo lee del ``LocalStatus`` del nodo.
    """
    if type(beacon_latency_ms_p95) is not int or not 0 <= beacon_latency_ms_p95 <= MAX_LATENCY_MS:
        raise ValueError("beacon_latency_ms_p95 son milisegundos enteros de 0 a una hora")
    return LatencyReport(
        node=TrancheMeasure(measured_by=MeasuredBy.INSTALLER, p95_ms=beacon_latency_ms_p95),
        platform=measure(upload, MeasuredBy.PLATFORM),
        exposure=measure(exposure, MeasuredBy.BROWSER),
        served=measure(served, MeasuredBy.PLATFORM),
    )
