"""Perfiles de la carga de exportación y de la volumetría de U-04 (TASK-353; NFR-LAZ-06, 09, 10 y
49; PAT-LAZ-ESC-05 y REN-03; infraestructura §6.2; A-65).

**Carga de exportación** (``ExportLoadProfile``, NFR-LAZ-49): una zona y 31 días a 77 hallazgos
por día (RNF-DES-03): 2 387 hallazgos, cada uno con un clip sintético. Se exporta dos veces
``with_clips``, con un paquete pequeño y otro **diez veces mayor**: solo cambia el tamaño de los
clips, así que lo único que crece entre las dos es el volumen de bytes que atraviesa el flujo.
Tamaños de producción en todos los perfiles: partes de 16 MiB y volúmenes de 2 GB
(``accreditation.export.volumes``).

- ``ci``: 32 MB frente a 320 MB (el check «backend (carga ci)» de cada PR);
- ``small``: 100 MB frente a 1 GB (en local, ``-k small``);
- ``nightly``: **1 GB frente a 10 GB** (trabajo ``carga`` de ``nightly.yml``).

Los clips viven en LocalStack como una **reserva** de ``clip_pool`` objetos sintéticos por tamaño:
varias evidencias apuntan al mismo objeto y cada una lo descarga entero con ``get_object``. Así el
paquete de 10 GB no exige 10 GB más de clips en el disco del runner (unos 20 GB libres).

Lo **funcional bloquea** (paquete ``ready``, verificador ``intact``, el mismo manifiesto en dos
corridas, ``/tmp`` por debajo de 32 MiB y la memoria que retiene el intento con el paquete grande
sin crecer más de ``MEMORY_GROWTH_LIMIT_KIB`` sobre la del pequeño); las cifras absolutas
(duración, memoria máxima del proceso y su crecimiento, y la memoria de la composición frente al
presupuesto de 512 MB) son **tendencia** en el informe (A-65).

**Volumetría** (``VolumetryProfile``, NFR-LAZ-06 y 09): un año completo de ``zones`` zonas de la
organización mayor a ritmo pleno (``LazRates``), extrapolado a 300 zonas, más ``notifications``
avisos de un solo destinatario para la paginación por clave. ``ci`` es lo mismo en miniatura.

Solo datos generados.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "ABSOLUTE_MEMORY_BUDGET_MIB",
    "EPISODES_PER_ZONE_DAY",
    "EXPORT_DAYS",
    "EXPORT_PROFILES",
    "MEMORY_GROWTH_LIMIT_KIB",
    "NFR_LAZ_06",
    "NFR_LAZ_06_TOTAL_BYTES",
    "TARGET_ZONES",
    "TMP_LIMIT_BYTES",
    "VOLUMETRY_PROFILES",
    "ExportLoadProfile",
    "LazRates",
    "NfrGroup",
    "VolumetryProfile",
]

MB: Final = 1000 * 1000
MIB: Final = 1024 * 1024
EPISODES_PER_ZONE_DAY: Final = 77
"""RNF-DES-03 (``[estimación propia]`` del diseño)."""
EXPORT_DAYS: Final = 31
TARGET_ZONES: Final = 300
"""RNF-DES-05: la escala objetivo a la que se extrapola la volumetría."""

TMP_LIMIT_BYTES: Final = 32 * MIB
"""``/tmp`` por debajo de 32 MiB (infraestructura §6.2, ``[objetivo propio]``): bloqueante."""
MEMORY_GROWTH_LIMIT_KIB: Final = 64 * 1024
"""El margen de NFR-LAZ-49, **fijado con la primera medición** (TASK-345, 2026-10-18: 200 MB, 1 GB
y 2,5 GB del compositor sintético sin crecer más de 64 MiB): la memoria que retiene el intento
(máximo de Python vivo a la vez) con el paquete grande no supera la del pequeño en más de esto. Es
lo que demuestra que el paquete se escribe en flujo; bloqueante. Medido con el compositor real el
2026-10-21: +2,8 MiB entre 100 MB y 1 GB."""
ABSOLUTE_MEMORY_BUDGET_MIB: Final = 512
"""Memoria de la composición por encima del uso base del proceso (infraestructura §6.2,
``[objetivo propio]``): se informa como tendencia (A-65), no decide el resultado. Primera medición
(2026-10-21, servidor de 4 núcleos): 99 MiB con 1 GB y 220 MiB con 10 GB."""


@dataclass(frozen=True)
class ExportLoadProfile:
    """Una zona, ``days`` días, ``findings_per_day`` hallazgos por día con un clip cada uno."""

    name: str
    small_bytes: int
    large_bytes: int
    days: int = EXPORT_DAYS
    findings_per_day: int = EPISODES_PER_ZONE_DAY
    clip_pool: int = 32
    """Objetos distintos de la reserva de clips en LocalStack, por tamaño."""
    child_timeout_seconds: float = 3_600.0
    """Tope del proceso hijo: nunca decide un resultado (una composición colgada es un fallo)."""

    @property
    def findings(self) -> int:
        return self.days * self.findings_per_day

    def clip_bytes(self, package_bytes: int) -> int:
        """Tamaño de cada clip para que los clips sumen ``package_bytes``."""
        return max(1, package_bytes // self.findings)


EXPORT_PROFILES: Final = {
    "ci": ExportLoadProfile("ci", small_bytes=32 * MB, large_bytes=320 * MB),
    "small": ExportLoadProfile("small", small_bytes=100 * MB, large_bytes=1_000 * MB),
    "nightly": ExportLoadProfile("nightly", small_bytes=1_000 * MB, large_bytes=10_000 * MB),
}


@dataclass(frozen=True)
class LazRates:
    """Filas por zona y día de cada tabla de U-04 ``[estimación propia]``.

    Salen de la tabla de NFR-LAZ-06 (filas al año a 300 zonas, entre 300 por 365): 77 episodios y
    14 detecciones por zona y día dan los 10 millones de ``FindingState``; 95 clasificaciones y 15
    resoluciones de revisión, los 12 millones; 82 avisos con 100 entregas, los 9 más 11 millones;
    12 correos y 2 lotes, el millón y medio; 4 acciones, 3 cierres, 1 declaración y 1 análisis, el
    «menos de un millón»; 1 contador de adopción, los 110 000. ``episode``, ``review_queue_item`` y
    ``notification_source_key`` no están en la tabla del diseño y se miden igual.
    """

    episodes: int = EPISODES_PER_ZONE_DAY
    detections: int = 14
    classifications: int = 95
    review_resolutions: int = 15
    notifications: int = 82
    deliveries: int = 100
    email_messages: int = 12
    digest_batches: int = 2
    corrective_actions: int = 4
    closure_records: int = 3
    unclosable_declarations: int = 1
    root_cause_analyses: int = 1
    adoption_counters: int = 1

    @property
    def finding_states(self) -> int:
        return self.episodes + self.detections


@dataclass(frozen=True)
class VolumetryProfile:
    name: str
    zones: int
    days: int
    notifications: int
    """Avisos de un solo destinatario para la paginación por clave (NFR-LAZ-09)."""
    page_size: int = 200
    rates: LazRates = field(default_factory=LazRates)

    @property
    def extrapolation(self) -> float:
        """Factor de la muestra a 300 zonas."""
        return TARGET_ZONES / self.zones


VOLUMETRY_PROFILES: Final = {
    "ci": VolumetryProfile(
        "ci",
        zones=2,
        days=7,
        notifications=20_000,
        rates=LazRates(
            episodes=8,
            detections=2,
            classifications=9,
            review_resolutions=2,
            notifications=8,
            deliveries=10,
            email_messages=2,
            digest_batches=1,
            corrective_actions=1,
            closure_records=1,
            unclosable_declarations=1,
            root_cause_analyses=1,
            adoption_counters=1,
        ),
    ),
    "nightly": VolumetryProfile("nightly", zones=30, days=365, notifications=1_000_000),
}


@dataclass(frozen=True)
class NfrGroup:
    """Una fila de la tabla de NFR-LAZ-06: sus tablas, filas al año y bytes al año estimados."""

    label: str
    tables: tuple[str, ...]
    rows_per_year: int
    bytes_per_year: int
    """Cota de la estimación (``menos de`` cuenta como el valor); 0 si el diseño dice
    «despreciable»."""


GB: Final = 1000 * MB
NFR_LAZ_06: Final = (
    NfrGroup("FindingState", ("loop.finding_state",), 10_000_000, 15 * GB),
    NfrGroup(
        "Classification más ReviewResolution",
        ("loop.classification", "loop.review_resolution"),
        12_000_000,
        12 * GB,
    ),
    NfrGroup(
        "Notification más NotificationDelivery",
        ("notifications.notification", "notifications.notification_delivery"),
        20_000_000,
        8 * GB,
    ),
    NfrGroup(
        "EmailMessage y DigestBatch",
        ("notifications.email_message", "notifications.digest_batch"),
        1_500_000,
        2 * GB,
    ),
    NfrGroup(
        "CorrectiveAction, ClosureRecord, UnclosableDeclaration, RootCauseAnalysis",
        (
            "loop.corrective_action",
            "loop.closure_record",
            "loop.unclosable_declaration",
            "loop.root_cause_analysis",
        ),
        1_000_000,
        2 * GB,
    ),
    NfrGroup("AdoptionCounter", ("accreditation.adoption_counter",), 110_000, 0),
)
"""La tabla de volumetría estimada de NFR-LAZ-06 (``[estimación propia]``); total de U-04 del
orden de 40 GB al año."""
NFR_LAZ_06_TOTAL_BYTES: Final = 40 * GB
