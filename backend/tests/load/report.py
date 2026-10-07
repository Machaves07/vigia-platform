"""Informe JSON de un perfil de carga (LC-GOB-22; NFR-GOB-02, 06, 18, 19, 50; PR-GOB-31).

Por perfil, un ``load-<perfil>-<semilla>.json`` en ``VIGIA_LOAD_REPORT_DIR`` (el artefacto que
``nightly`` conserva 90 días) o, sin ella, en el directorio temporal de la sesión, con:

- la semilla y el perfil (flota y fases);
- conteos de eventos del cliente por operación, resultado y ``rejection_code``, recibos
  ``accepted`` y ``accepted_duplicate``, respuestas perdidas y cola muerta;
- tasas: escrituras por segundo en régimen y durante cada vaciado de cola, agregadas y por cadena
  de planta (NFR-GOB-02, tendencia fuera del ``soak``), tiempo de vaciado y la peor proporción de
  rechazos permanentes en 15 minutos (NFR-GOB-50);
- latencia por ruta del contrato (NFR-GOB-01, tendencia) y p95 por ruta del cliente sintético de
  consola (NFR-GOB-03 y 19);
- ocupación de los mamparos por clase de ruta (``bulkhead_in_use``, ``bulkhead_size``,
  ``bulkhead_wait_ms`` y ``bulkhead_rejected_total`` de la plataforma, NFR-GOB-19).

Nunca identificadores de registros, cuerpos, URL prefirmadas, PEM ni códigos de alta:
``secret_findings`` lo comprueba sobre el informe y las salidas (PR-GOB-31).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)

__all__ = [
    "REPORT_DIR_VARIABLE",
    "SECRET_MARKERS",
    "BulkheadSampler",
    "Drain",
    "analyse",
    "latency_summary",
    "percentile",
    "secret_findings",
    "write_report",
]

REPORT_DIR_VARIABLE: Final = "VIGIA_LOAD_REPORT_DIR"
SECRET_MARKERS: Final = (
    "-----BEGIN",
    "PRIVATE KEY",
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
)
"""Un PEM o una URL prefirmada (SigV4 por consulta) en cualquier forma (PR-GOB-31)."""
PERMANENT_WINDOW: Final = dt.timedelta(minutes=15)
PERMANENT_THRESHOLD: Final = 0.01
"""Alarma de rechazos permanentes de NFR-GOB-46: más del 1 % de la ingesta en 15 minutos."""
TRAILING: Final = dt.timedelta(seconds=60)
MINIMUM_PER_MINUTE: Final = {"submit_record": 60, "request_grant": 240}
"""NFR-CTR-02: por debajo, un ``rate_limited`` es un defecto de la plataforma."""
SAMPLE_SECONDS: Final = 1.0


def percentile(values: Sequence[float], q: float) -> float | None:
    """Percentil ``q`` (0 a 100) por rango más cercano; ``None`` sin valores."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return float(ordered[rank - 1])


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def latency_summary(values: Sequence[float], statuses: Mapping[int, int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_ms": _round(percentile(values, 50)),
        "p95_ms": _round(percentile(values, 95)),
        "p99_ms": _round(percentile(values, 99)),
        "max_ms": _round(max(values) if values else None),
        "statuses": {str(status): count for status, count in sorted(statuses.items())},
    }


def _parse(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# --- Análisis del resultado de los nodos ---------------------------------------------------------


@dataclass(frozen=True)
class Drain:
    """El vaciado de la cola acumulada en una fase ``unreachable``."""

    phase: str
    queued: int
    accepted: int
    seconds: float | None
    writes_per_second: float | None
    plant_writes_per_second: Mapping[str, float]

    def to_json(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "queued": self.queued,
            "accepted": self.accepted,
            "seconds": _round(self.seconds),
            "writes_per_second": _round(self.writes_per_second),
            "max_plant_chain_writes_per_second": _round(
                max(self.plant_writes_per_second.values(), default=None)
            ),
        }


def _drain(phase: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]) -> Drain:
    """Lo emitido durante la fase y cuánto tardó en aceptarse desde que la plataforma volvió."""
    start, end = _parse(phase["start"]), _parse(phase["end"])
    queued = [record for record in records.values() if start <= _parse(record["emitted_at"]) < end]
    accepted = [_parse(record["accepted_at"]) for record in queued if record["accepted_at"]]
    seconds = rate = None
    if accepted and len(accepted) == len(queued):
        seconds = max((max(accepted) - end).total_seconds(), 0.001)
        rate = len(queued) / seconds
    by_plant: dict[str, list[dt.datetime]] = defaultdict(list)
    for record in queued:
        if record["accepted_at"]:
            by_plant[record["plant_id"]].append(_parse(record["accepted_at"]))
    plant_rates = {
        plant: len(times) / max((max(times) - end).total_seconds(), 0.001)
        for plant, times in by_plant.items()
    }
    return Drain(phase["name"], len(queued), len(accepted), seconds, rate, plant_rates)


def _steady_rate(result: Mapping[str, Any]) -> float | None:
    steady = next((phase for phase in result["phases"] if phase["name"] == "steady"), None)
    if steady is None:
        return None
    start, end = _parse(steady["start"]), _parse(steady["end"])
    accepted = [
        record
        for record in result["records"].values()
        if record["accepted_at"] and start <= _parse(record["accepted_at"]) < end
    ]
    return len(accepted) / max((end - start).total_seconds(), 0.001)


def _limited_below_minimum(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cada ``rate_limited`` de un registro o una concesión cuando el nodo llevaba en el último
    minuto no más envíos de esa clase que el mínimo de NFR-CTR-02."""
    sends: dict[str, list[tuple[str, str]]] = result["sends"]
    below = []
    for event in result["rate_limited"]:
        minimum = MINIMUM_PER_MINUTE.get(event["operation"])
        if minimum is None:
            continue
        at = _parse(event["at"])
        recent = sum(
            1
            for operation, stamp in sends.get(str(event["node_index"]), [])
            if operation == event["operation"] and at - TRAILING <= _parse(stamp) <= at
        )
        if recent <= minimum:
            below.append({**event, "sent_last_minute": recent, "minimum": minimum})
    return below


def _worst_permanent_ratio(result: Mapping[str, Any]) -> float:
    """La mayor proporción de registros en cola muerta frente a los emitidos en una ventana de
    15 minutos (por instante de emisión)."""
    rejected = set(result["journal"]["rejected"])
    emitted = sorted(
        (_parse(record["emitted_at"]), record_id in rejected)
        for record_id, record in result["records"].items()
    )
    worst = 0.0
    start = 0
    dead = 0
    for end, (moment, is_dead) in enumerate(emitted):
        dead += is_dead
        while emitted[start][0] < moment - PERMANENT_WINDOW:
            dead -= emitted[start][1]
            start += 1
        worst = max(worst, dead / (end - start + 1))
    return worst


def analyse(result: Mapping[str, Any]) -> dict[str, Any]:
    """Conteos y tasas del resultado del proceso de los nodos (sin identificadores)."""
    journal = result["journal"]
    events: Counter[str] = Counter()
    by_code: Counter[str] = Counter()
    for operation, outcome, code, count in result["events"]:
        events[f"{operation}:{outcome}"] += count
        if code:
            by_code[f"{operation}:{code}"] += count
    drains = [
        _drain(phase, result["records"]) for phase in result["phases"] if phase["unreachable"]
    ]
    return {
        "planned": journal["planned"],
        "emitted": len(journal["emitted"]),
        "accepted": len(journal["accepted"]),
        "lost": len(set(journal["emitted"]) - set(journal["accepted"])),
        "dead_letter": dict(Counter(journal["rejected"].values())),
        "emitted_by_kind": journal["emitted_by_kind"],
        "receipts": result["receipts"],
        "lost_replies": result["lost_replies"],
        "events": dict(sorted(events.items())),
        "rejection_codes": dict(sorted(by_code.items())),
        "rate_limited_below_minimum": len(_limited_below_minimum(result)),
        "rate_limited_by_operation": dict(
            Counter(event["operation"] for event in result["rate_limited"])
        ),
        "worst_permanent_ratio_15min": round(_worst_permanent_ratio(result), 4),
        "steady_writes_per_second": _round(_steady_rate(result)),
        "drains": [drain.to_json() for drain in drains],
        "halted": result["halted"],
        "node_latency": result["latency"],
    }


def drains_of(result: Mapping[str, Any]) -> list[Drain]:
    return [_drain(phase, result["records"]) for phase in result["phases"] if phase["unreachable"]]


# --- Mamparos -------------------------------------------------------------------------------------


@dataclass
class BulkheadSampler:
    """Lee cada segundo los medidores de mamparo de la plataforma (``InMemoryMetricReader``) y
    guarda, por clase, el máximo y la media de ``bulkhead_in_use`` y su ``bulkhead_size``."""

    reader: InMemoryMetricReader
    in_use: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    size: dict[str, float] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mamparos", daemon=True)
        self._thread.start()

    def _points(self) -> Iterable[tuple[str, Any]]:
        data = self.reader.get_metrics_data()
        if data is None:
            return
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        yield metric.name, point

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_SECONDS):
            self.sample()

    def sample(self) -> None:
        for name, point in self._points():
            if not isinstance(point, NumberDataPoint):
                continue
            pool = str((point.attributes or {}).get("pool_class"))
            if name == "bulkhead_in_use":
                self.in_use[pool].append(float(point.value))
            elif name == "bulkhead_size":
                self.size[pool] = float(point.value)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self.sample()
        waits: dict[str, dict[str, Any]] = {}
        rejected: Counter[str] = Counter()
        for name, point in self._points():
            pool = str((point.attributes or {}).get("pool_class"))
            if name == "bulkhead_wait_ms" and isinstance(point, HistogramDataPoint):
                waits[pool] = {
                    "count": point.count,
                    "mean_ms": _round(point.sum / point.count if point.count else None),
                    "max_ms": _round(point.max if point.count else None),
                }
            elif name == "bulkhead_rejected_total" and isinstance(point, NumberDataPoint):
                rejected[pool] += int(point.value)
        occupancy: dict[str, Any] = {}
        for pool in sorted(set(self.in_use) | set(self.size)):
            values = self.in_use.get(pool, [])
            size = self.size.get(pool)
            peak = max(values, default=None)
            occupancy[pool] = {
                "size": size,
                "max_in_use": peak,
                "mean_in_use": _round(sum(values) / len(values) if values else None),
                "max_occupancy_percent": _round(
                    peak / size * 100 if peak is not None and size else None
                ),
                "wait_ms": waits.get(pool),
                "rejected": rejected.get(pool, 0),
            }
        return occupancy


# --- Secretos y escritura -------------------------------------------------------------------------


def secret_findings(texts: Mapping[str, str], codes: Sequence[str] = ()) -> list[str]:
    """Qué salidas contienen un código de alta, un PEM o una URL prefirmada: ``nombre: motivo``,
    nunca el valor (PR-GOB-31). Vacío si ninguna."""
    found = []
    for name, text in texts.items():
        if any(code in text for code in codes):
            found.append(f"{name}: código de alta")
        lowered = text.lower()
        for marker in SECRET_MARKERS:
            if marker.lower() in lowered:
                found.append(f"{name}: {marker}")
    return found


def report_directory(fallback: Path) -> Path:
    configured = os.environ.get(REPORT_DIR_VARIABLE)
    directory = Path(configured) if configured else fallback
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_report(directory: Path, profile: str, seed: int, document: Mapping[str, Any]) -> Path:
    path = directory / f"load-{profile}-{seed}.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
