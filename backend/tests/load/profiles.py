"""Perfiles de carga ``ci``, ``nightly`` y ``soak`` (LC-GOB-22; NFR-GOB-06, 02, 18, 19, 50).

Un ``LoadProfile`` fija la flota (nodos, zonas por nodo, nodos por planta), la aceleración del
nodo simulado de U-01 y sus **fases** en tiempo simulado:

- ``steady``: ritmo del piloto (77 episodios por día y zona, RNF-DES-03) durante
  ``steady_minutes`` reales;
- ``reconnection``: la plataforma deja de ser alcanzable durante ``queue_hours`` simuladas (los
  nodos encolan) y vuelve: los nodos vacían esa cola **a la vez** (reconexión masiva de
  NFR-GOB-02 y 50); el cliente sintético de consola mide en paralelo (NFR-GOB-19);
- ``outage``: ``outage_hours`` simuladas de indisponibilidad seguidas de la reconexión
  (NFR-GOB-18);
- entre fases y al final, ``settle_hours`` simuladas para que la cola se vacíe.

``ci`` (NFR-GOB-06): 10 nodos de una zona en una planta, ``speed_factor`` 60 durante 5 minutos,
solo funcional e idempotencia (PR-GOB-01): una fracción de las respuestas a los registros se
pierde después de que la plataforma los aceptó y el nodo los reenvía con la misma clave.
``nightly``: 100 nodos y 300 zonas (5 plantas de 20 nodos y 60 zonas, la organización mayor de
NFR-GOB-11), ``speed_factor`` 60 durante 30 minutos, más la reconexión de una hora de cola y las
4 horas de indisponibilidad. ``smoke`` es el
``nightly`` reducido para ensayar el arnés en el PC compartido (``VIGIA_LOAD_SCALE=smoke``: 20
nodos, a lo sumo 3 minutos): las mismas fases a ``speed_factor`` 240.

Los objetivos **absolutos** de NFR-GOB-01 a 05 (``ABSOLUTE_TARGETS``) son tendencia en ``ci`` y
``nightly`` y umbral bloqueante en ``soak`` (infrastructure-design §9.2). El conjunto sellado del
nodo simulado es sintético (``write_sealed_dataset``): imágenes generadas y un manifiesto sellado
con la verificación superada, nunca un histórico real (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import fractions
import hashlib
import json
import math
import os
import random
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import av
import numpy as np
from vigia_contracts.conformance.simulated_node.dataset import (
    DATASET_DIR,
    EVENTS_NAME,
    MANIFEST_NAME,
)
from vigia_contracts.conformance.simulated_node.profile import (
    DEFAULT_EPISODES_PER_DAY,
    parse_profile,
)
from vigia_contracts.models.simulation_profile import SimulationProfile

__all__ = [
    "ABSOLUTE_TARGETS",
    "HOUR_MS",
    "PROFILES",
    "SCALE_VARIABLE",
    "LoadProfile",
    "Phase",
    "nightly_profile",
    "simulation_profile",
    "write_sealed_dataset",
]

HOUR_MS: Final = 3_600_000
MINUTE_MS: Final = 60_000
SCALE_VARIABLE: Final = "VIGIA_LOAD_SCALE"
"""``full`` (por defecto) o ``smoke``: la escala del perfil ``nightly``."""
IMAGES: Final = 30
"""Imágenes del conjunto sintético: la muestra de verificación del contrato es de al menos 30."""


@dataclass(frozen=True)
class Phase:
    """Una fase del perfil en milisegundos simulados desde el inicio; ``unreachable`` si la
    plataforma no responde durante ella (los nodos encolan)."""

    name: str
    start_ms: int
    end_ms: int
    unreachable: bool = False


@dataclass(frozen=True)
class LoadProfile:
    name: str
    nodes: int
    zones_per_node: int
    nodes_per_plant: int
    speed_factor: float
    steady_minutes: float
    queue_hours: float = 0.0
    outage_hours: float = 0.0
    settle_hours: float = 0.0
    lost_reply_fraction: float = 0.0
    """Fracción de respuestas a registros que se pierden tras aceptarlos (PR-GOB-01)."""
    console: bool = False
    """El cliente sintético de consola mide durante las reconexiones (NFR-GOB-19)."""
    episodes_per_day: int = DEFAULT_EPISODES_PER_DAY

    def __post_init__(self) -> None:
        if not 1 <= self.nodes_per_plant <= self.nodes:
            raise ValueError("nodes_per_plant va de 1 al número de nodos")
        if not 1 <= self.zones_per_node <= 16:
            raise ValueError("un nodo atiende de 1 a 16 zonas")
        if not 0.0 <= self.lost_reply_fraction < 1.0:
            raise ValueError("lost_reply_fraction va de 0 a 1 (sin incluirlo)")

    @property
    def zones(self) -> int:
        return self.nodes * self.zones_per_node

    @property
    def plants(self) -> int:
        return math.ceil(self.nodes / self.nodes_per_plant)

    def phases(self) -> tuple[Phase, ...]:
        """Las fases en orden, sin huecos: ``steady``, y si las hay, la cola y la caída, cada una
        seguida de su ``settle``."""
        cursor = round(self.steady_minutes * MINUTE_MS * self.speed_factor)
        phases = [Phase("steady", 0, cursor)]
        settle = round(self.settle_hours * HOUR_MS)
        for name, hours in (("reconnection", self.queue_hours), ("outage", self.outage_hours)):
            if hours <= 0:
                continue
            length = round(hours * HOUR_MS)
            phases.append(Phase(name, cursor, cursor + length, unreachable=True))
            cursor += length
            if settle:
                phases.append(Phase(f"{name}-settle", cursor, cursor + settle))
                cursor += settle
        return tuple(phases)

    @property
    def span_ms(self) -> int:
        """Milisegundos simulados de la ejecución entera."""
        return self.phases()[-1].end_ms

    @property
    def wall_seconds(self) -> float:
        """Duración real de las fases (sin aprovisionamiento ni vaciado final)."""
        return self.span_ms / self.speed_factor / 1000

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "nodes": self.nodes,
            "zones": self.zones,
            "zones_per_node": self.zones_per_node,
            "plants": self.plants,
            "speed_factor": self.speed_factor,
            "episodes_per_day": self.episodes_per_day,
            "steady_minutes": self.steady_minutes,
            "queue_hours": self.queue_hours,
            "outage_hours": self.outage_hours,
            "settle_hours": self.settle_hours,
            "lost_reply_fraction": self.lost_reply_fraction,
            "wall_seconds": round(self.wall_seconds, 1),
            "phases": [
                {
                    "name": phase.name,
                    "start_ms": phase.start_ms,
                    "end_ms": phase.end_ms,
                    "unreachable": phase.unreachable,
                }
                for phase in self.phases()
            ],
        }


PROFILES: Final[Mapping[str, LoadProfile]] = {
    "ci": LoadProfile(
        "ci",
        nodes=10,
        zones_per_node=1,
        nodes_per_plant=10,
        speed_factor=60.0,
        steady_minutes=5.0,
        lost_reply_fraction=0.1,
    ),
    "nightly": LoadProfile(
        "nightly",
        nodes=100,
        zones_per_node=3,
        nodes_per_plant=20,
        speed_factor=60.0,
        steady_minutes=30.0,
        queue_hours=1.0,
        outage_hours=4.0,
        settle_hours=0.5,
        lost_reply_fraction=0.02,
        console=True,
    ),
    "smoke": LoadProfile(
        "smoke",
        nodes=20,
        zones_per_node=3,
        nodes_per_plant=20,
        speed_factor=240.0,
        steady_minutes=0.5,
        queue_hours=1.0,
        outage_hours=4.0,
        settle_hours=0.5,
        lost_reply_fraction=0.02,
        console=True,
    ),
    "soak": LoadProfile(
        "soak",
        nodes=100,
        zones_per_node=3,
        nodes_per_plant=20,
        speed_factor=1.0,
        steady_minutes=480.0,
        console=True,
    ),
}
"""``smoke``: 2 h de régimen, 1 h de cola y 4 h de caída en 2 minutos reales (≤ 3 min).
``soak``: 8 horas a ritmo real contra ``staging-<n>`` (``soak.py``); la flota es la del entorno."""


def nightly_profile() -> LoadProfile:
    """El perfil ``nightly`` a la escala de ``VIGIA_LOAD_SCALE`` (``full`` por defecto)."""
    scale = os.environ.get(SCALE_VARIABLE, "full").strip().lower() or "full"
    if scale == "full":
        return PROFILES["nightly"]
    if scale == "smoke":
        return PROFILES["smoke"]
    raise ValueError(f"{SCALE_VARIABLE} es full o smoke, no {scale!r}")


# --- Objetivos absolutos (NFR-GOB-01 a 05) --------------------------------------------------------


ABSOLUTE_TARGETS: Final[Mapping[str, Mapping[str, Any]]] = {
    "NFR-GOB-01": {
        "POST /api/nodes/findings": {"p95_ms": 500, "p99_ms": 1_500},
        "POST /api/nodes/detection-reviews": {"p95_ms": 500, "p99_ms": 1_500},
        "POST /api/nodes/observability-events": {"p95_ms": 200},
        "POST /api/nodes/heartbeats": {"p95_ms": 150},
        "POST /api/nodes/clip-uploads": {"p95_ms": 100},
        "GET /api/nodes/zones/{zone_id}/catalog": {"p95_ms": 100},
        "POST /api/nodes/enrollment": {"p95_ms": 2_000},
    },
    "NFR-GOB-02": {"aggregate_writes_per_second": 100, "plant_chain_writes_per_second": 20},
    "NFR-GOB-03": {
        "GET /fleet/nodes": {"p95_ms": 500},
        "GET /fleet/nodes/{node_id}": {"p95_ms": 300},
        "GET /zones/{zone_id}/walk-tests/current": {"p95_ms": 200},
        "GET /zones/{zone_id}/catalog": {"p95_ms": 200},
        "GET /zones/{zone_id}/catalog/versions": {"p95_ms": 200},
        "GET /zones/{zone_id}/gates": {"p95_ms": 200},
        "GET /zones/{zone_id}/transparency": {"p95_ms": 200},
        "GET /commissioning-records/{record_id}": {"p95_ms": 300},
        "POST /documents": {"p95_ms": 300},
    },
    "NFR-GOB-04": {
        "single_port_call": {"p95_ms": 5},
        "batch_port_call": {"p95_ms": 50},
        "measured_by": "banco en proceso de VIG-170: los puertos no tienen ruta",
    },
    "NFR-GOB-05": {"commissioning_document": {"p95_ms": 3_000}},
}
"""Objetivos ``[objetivos propios]`` de nfr-requirements §1 de U-03, por ruta o puerto."""


# --- Perfil del nodo simulado ---------------------------------------------------------------------


def simulation_profile(seed: int, speed_factor: float, episodes_per_day: int) -> SimulationProfile:
    """El ``SimulationProfile`` de la ejecución, leído en modo estricto por el lector del kit:
    77 episodios por día y zona, una cuarta parte en la banda de revisión, señal energizada en
    horario laboral y una obstrucción de cámara de 10 minutos a la hora 1 (eventos de
    observabilidad)."""
    document = {
        "episodes_per_day": episodes_per_day,
        "review_band_fraction": 0.25,
        "known_false_positive_names": [],
        "observability_schedule": [
            {
                "at_offset_ms": HOUR_MS,
                "subject_kind": "camera",
                "causes": ["obstruction"],
                "duration_ms": 10 * MINUTE_MS,
            }
        ],
        "signal_profile": "energized_business_hours",
        "speed_factor": float(speed_factor),
        "seed": seed,
    }
    return parse_profile(json.dumps(document).encode("utf-8"))


# --- Conjunto sellado sintético -------------------------------------------------------------------


def _jpeg(seed: int, index: int, width: int = 640, height: int = 480) -> bytes:
    """Una imagen JPEG generada (degradado con ruido determinista); ninguna persona ni foto."""
    rng = np.random.default_rng(seed + index)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(255, 0, height, dtype=np.uint8)[:, None]
    frame[:, :, 2] = rng.integers(0, 64, size=(height, width), dtype=np.uint8)
    codec = av.CodecContext.create("mjpeg", "w")
    codec.width, codec.height = width, height
    codec.pix_fmt = "yuvj420p"
    codec.time_base = fractions.Fraction(1, 1)
    picture = av.VideoFrame.from_ndarray(frame, format="rgb24").reformat(format="yuvj420p")
    return b"".join(bytes(packet) for packet in [*codec.encode(picture), *codec.encode(None)])


def _stamp(moment: dt.datetime) -> str:
    utc = moment.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _uuid7(rng: random.Random, moment: dt.datetime) -> str:
    milliseconds = int(moment.timestamp() * 1000)
    value = (milliseconds << 80) | (0x7 << 76) | (rng.getrandbits(12) << 64)
    value |= (0b10 << 62) | rng.getrandbits(62)
    return str(uuid.UUID(int=value))


def write_sealed_dataset(root: Path, seed: int, now: dt.datetime) -> Path:
    """Escribe en ``root`` un conjunto sellado sintético que ``load_sealed_dataset`` acepta:
    ``IMAGES`` imágenes generadas, la copia del registro tabulado y el manifiesto sellado con la
    verificación visual superada. Devuelve ``root``."""
    rng = random.Random(seed)  # noqa: S311 - datos sintéticos
    images_dir = root / DATASET_DIR
    images_dir.mkdir(parents=True, exist_ok=True)
    events = b"timestamp,count_id,total,in_zone\n"
    (root / EVENTS_NAME).write_bytes(events)
    signer = {"display_name": "Responsable sintético", "role": "product_owner"}
    entries = []
    for index in range(IMAGES):
        name = f"img_{index + 1:04d}.jpg"
        data = _jpeg(seed, index)
        (images_dir / name).write_bytes(data)
        entries.append(
            {
                "source_name": f"src_{index + 1:04d}.jpg",
                "output_name": name,
                "output_sha256": hashlib.sha256(data).hexdigest(),
                "action": "full_blur",
                "boxes": [],
                "label_regions": [],
                "review": {"required": False},
                "event_row": {
                    "timestamp": _stamp(now - dt.timedelta(days=1, minutes=index)),
                    "count_id": index,
                    "total": 0,
                    "in_zone": 0,
                },
            }
        )
    manifest = {
        "dataset_id": _uuid7(rng, now),
        "source_description_es": "Conjunto sintético generado para las pruebas de carga",
        "created_at": _stamp(now),
        "sealed_at": _stamp(now),
        "sealed_by": signer,
        "image_count": IMAGES,
        "images": entries,
        "events_csv_sha256": hashlib.sha256(events).hexdigest(),
        "verification": {
            "sample_size": IMAGES,
            "sample_seed": seed,
            "sampled_output_names": [entry["output_name"] for entry in entries],
            "reviewed_all_unboxed": True,
            "verifier": signer,
            "verified_at": _stamp(now),
            "result": "passed",
        },
        "tool_version": "1.0.0",
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root
