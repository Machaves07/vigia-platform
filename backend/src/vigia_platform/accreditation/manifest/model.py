"""``ExportManifest`` y sus piezas (LC-LAZ-24; DE §3.2 con las notas U04-H-03 y H-08; BR-LAZ-88 a
99; NFR Requirements, nota de BR-LAZ-90).

Lo que un tercero lee primero y la razón por la que **nada falta en silencio**:

- una sola organización y una sola planta (BR-LAZ-88);
- los **conteos por estado** de todos los hallazgos del periodo (abiertos, cerrados, incerrables,
  descartados, suspendidos y con evidencia marcada; BR-LAZ-89 y 94);
- cada artefacto con su hash y su tamaño, y las entradas **declaradas ausentes** con su motivo
  (el documento portátil y el verificador hasta TASK-319 y TASK-346): nunca se omiten;
- ``package_form`` y, en la forma sin clips, el aviso fijo y la lista de clips con hash, duración
  y zona (nota de BR-LAZ-90);
- puntos de control y claves públicas vigentes y retiradas (BR-LAZ-91, BR-NUC-55);
- el resumen de cobertura **con sus huecos** y el aviso fijo «no se observó» (BR-LAZ-93);
- los estándares citados con su texto y su hash (BR-LAZ-95), los acuerdos por zona con firmantes
  y sustituciones (BR-LAZ-96), los tramos de acta no vigente (BR-LAZ-97), los clips frente a los
  adjuntos declarados (BR-LAZ-98) y las zonas unipersonales con marcas agregadas (BR-LAZ-99).

**Textos fijos**: toda frase del manifiesto que no es dato de otra unidad sale de
``FIXED_TEXTS_ES``, una lista **cerrada**. Ninguna afirma que no ocurrió nada: dicen qué se
observó y qué no. Las pruebas comparan por **igualdad** contra esta lista, nunca por subcadena
(VIG-194).

Módulo puro: sin FastAPI, sin SQLAlchemy, sin E/S y sin hora del sistema.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

__all__ = [
    "ABSENT_KINDS",
    "AGGREGATION_NOTICE_ES",
    "ANONYMIZATION_STATEMENT_ES",
    "ASSIGNMENT_HISTORY_UNAVAILABLE_ES",
    "COVERAGE_NOTICE_ES",
    "DOCUMENT_ABSENT_ES",
    "EVIDENCE_FLAGGED_NOTICE_ES",
    "FIXED_TEXTS_ES",
    "MANIFEST_VERSION",
    "NOT_ELAPSED_CAUSE",
    "REGRESSION_NOTICE_ES",
    "REVOCATION_NOTICE_ES",
    "VERIFIER_ABSENT_ES",
    "VERIFIER_VERSION_ABSENT",
    "WITHOUT_CLIPS_NOTICE_ES",
    "WITH_CLIPS_NOTICE_ES",
    "AbsentEntry",
    "ActorSnapshot",
    "AgreementEntry",
    "AgreementSignatoryEntry",
    "ArtifactEntry",
    "AssignmentEntry",
    "CheckpointEntry",
    "CoverageZoneSummary",
    "DeclaredAttachment",
    "DeclaredClip",
    "ExportManifest",
    "FindingSummary",
    "GateIntervalEntry",
    "ManifestInputs",
    "OrganizationRef",
    "PlantRef",
    "RegressionPeriod",
    "RevokedGate",
    "StandardCited",
    "StateCounts",
    "ZoneEntry",
]

MANIFEST_VERSION: Final = 1

VERIFIER_VERSION_ABSENT: Final = "absent"
"""``verifier_version`` mientras el paquete no lleva el verificador (TASK-346 lo rellena)."""

NOT_ELAPSED_CAUSE: Final = "period_not_elapsed"
"""Causa del tramo final de un periodo que termina después de ``generated_at``: ese tiempo aún
no había pasado y por tanto **no se observó** (nunca se presenta como observado)."""

ABSENT_KINDS: Final = ("document", "verifier")
"""Entradas que el paquete puede declarar ausentes (nunca se omiten en silencio)."""

# --- Textos fijos (lista cerrada) ---------------------------------------------------------------

COVERAGE_NOTICE_ES: Final = (
    "Los tramos no observables o degradados de la línea de tiempo son tiempo en que Vigía no "
    "observó la zona, o la observó con limitaciones. De esos tramos el paquete solo afirma que no "
    "se observó: no dice qué pasó en ellos."
)
"""BR-LAZ-93: «no se observó», nunca «no ocurrió»."""

ANONYMIZATION_STATEMENT_ES: Final = (
    "Los artefactos de tipo clip son clips difuminados por el nodo, con la marca de anonimización "
    "comprobada por la plataforma al recibirlos. Los de tipo attachment son adjuntos subidos por "
    "personas bajo una declaración firmada de que no muestran a nadie identificable; la "
    "plataforma no puede verificar esa declaración."
)
"""BR-LAZ-98: distingue el clip verificado del adjunto declarado (P6)."""

WITHOUT_CLIPS_NOTICE_ES: Final = (
    "Este paquete es de la forma sin clips: cada clip se lista con su hash, su duración y su zona, "
    "pero sus bytes no van incluidos. Para recibirlos, pida una exportación con clips del mismo "
    "alcance."
)
"""Nota de BR-LAZ-90: el aviso fijo de la forma sin clips."""

WITH_CLIPS_NOTICE_ES: Final = (
    "Este paquete es de la forma con clips: incluye los bytes de cada clip listado, con su hash."
)

EVIDENCE_FLAGGED_NOTICE_ES: Final = (
    "Los hallazgos marcados con evidence_flagged tienen algún clip cuya verificación diferida de "
    "la marca de anonimización falló. Siguen en el expediente y van en el paquete con su marca: "
    "no se omiten."
)
"""BR-LAZ-94."""

REGRESSION_NOTICE_ES: Final = (
    "Los tramos con acta de walk-test no vigente se declaran por zona con su causa. La zona siguió "
    "en modo productivo: la anotación informa, no descalifica."
)
"""BR-LAZ-97."""

AGGREGATION_NOTICE_ES: Final = (
    "En las zonas de ocupación unipersonal, las marcas de tiempo de los hallazgos se presentan "
    "agregadas al inicio de su ventana para no señalar a una persona; esos registros se listan "
    "por su hash, sin su contenido. El expediente y su cadena de hashes conservan la marca exacta."
)
"""BR-LAZ-99: presente solo si alguna zona es unipersonal."""

REVOCATION_NOTICE_ES: Final = (
    "Alguna zona del paquete tuvo la compuerta de uso revocada durante el periodo. La zona se "
    "exporta con esta advertencia y nunca se omite; cada revocación figura con su fecha y con el "
    "motivo que declaró la gobernanza."
)
"""BR-LAZ-96: presente solo si alguna zona tuvo una revocación en el periodo."""

DOCUMENT_ABSENT_ES: Final = (
    "El documento portátil legible todavía no se genera en esta versión de la plataforma: la "
    "entrada se declara ausente."
)
VERIFIER_ABSENT_ES: Final = (
    "El verificador de cadena todavía no viaja dentro del paquete en esta versión de la "
    "plataforma: la entrada se declara ausente."
)
ASSIGNMENT_HISTORY_UNAVAILABLE_ES: Final = (
    "La historia de asignación de nodos de la zona no estaba disponible al componer el paquete: "
    "se declara ausente."
)

FIXED_TEXTS_ES: Final = (
    COVERAGE_NOTICE_ES,
    ANONYMIZATION_STATEMENT_ES,
    WITHOUT_CLIPS_NOTICE_ES,
    WITH_CLIPS_NOTICE_ES,
    EVIDENCE_FLAGGED_NOTICE_ES,
    REGRESSION_NOTICE_ES,
    AGGREGATION_NOTICE_ES,
    REVOCATION_NOTICE_ES,
    DOCUMENT_ABSENT_ES,
    VERIFIER_ABSENT_ES,
    ASSIGNMENT_HISTORY_UNAVAILABLE_ES,
)
"""Lista **cerrada** de las frases que el manifiesto escribe por su cuenta."""


# --- Piezas -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class OrganizationRef:
    organization_id: uuid.UUID
    code: str
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PlantRef:
    plant_id: uuid.UUID
    code: str
    name: str
    country: str
    timezone: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ActorSnapshot:
    """``generated_by``: la instantánea de quien pidió la exportación."""

    user_id: str
    display_name: str | None
    role_in_use: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class GateIntervalEntry:
    """Un intervalo de ``gate_history`` (montaje o uso) que se solapa con el periodo."""

    gate: str
    status: str
    effective_from: datetime
    effective_until: datetime | None
    decided_by: uuid.UUID
    reason_es: str | None
    record_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgreementSignatoryEntry:
    role: str
    user_id: uuid.UUID
    confirmed_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgreementEntry:
    """La vigencia de un acuerdo de uso en el periodo (BR-LAZ-96)."""

    agreement_id: uuid.UUID
    record_id: uuid.UUID
    status: str
    approved_at: datetime
    approved_by: uuid.UUID
    state_in_period: str
    """``current``, ``superseded`` o ``revoked``."""
    since: datetime
    """Desde cuándo está en ese estado (la aprobación si ``current``)."""
    replaces_agreement_id: uuid.UUID | None
    signatories: tuple[AgreementSignatoryEntry, ...]
    document_sha256: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class AssignmentEntry:
    """Una asignación de nodo de ``assignment_history``."""

    node_id: uuid.UUID | None
    assigned_at: datetime
    unassigned_at: datetime | None
    replaces_node_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneEntry:
    zone_id: uuid.UUID
    code: str
    name: str
    gate_intervals: tuple[GateIntervalEntry, ...]
    agreements: tuple[AgreementEntry, ...]
    node_assignments: tuple[AssignmentEntry, ...] | None
    """``None`` si la historia no estaba disponible: se declara ausente con su motivo."""
    single_occupancy: bool
    aggregation_window_minutes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageZoneSummary:
    """El resumen de la línea de tiempo de una zona: suma exactamente la duración del periodo."""

    zone_id: uuid.UUID
    observable_ms: int
    degraded_ms: int
    not_observable_ms: int
    no_communication_ms: int
    never_reported_ms: int
    zone_not_active_ms: int
    not_elapsed_ms: int
    interval_count: int

    @property
    def total_ms(self) -> int:
        return (
            self.observable_ms
            + self.degraded_ms
            + self.not_observable_ms
            + self.no_communication_ms
            + self.never_reported_ms
            + self.zone_not_active_ms
            + self.not_elapsed_ms
        )

    @property
    def unobserved_ms(self) -> int:
        """Lo que no se observó plenamente: todo menos ``observable``."""
        return self.total_ms - self.observable_ms


@dataclass(frozen=True, slots=True, kw_only=True)
class RegressionPeriod:
    """Un tramo con acta de walk-test no vigente (BR-LAZ-97), recortado al periodo."""

    zone_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime | None
    cause: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokedGate:
    """Una revocación de la compuerta de uso que se solapa con el periodo (BR-LAZ-96)."""

    zone_id: uuid.UUID
    since: datetime
    until: datetime | None
    reason_es: str | None
    record_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class StandardCited:
    """La versión de un estándar con la que se evaluó algún hallazgo (BR-LAZ-95)."""

    zone_id: uuid.UUID
    standard_id: uuid.UUID
    version: int
    title_es: str
    declared_text: str
    declared_text_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeclaredClip:
    """Un clip difuminado por el nodo (BR-LAZ-98): con sus bytes o solo declarado."""

    evidence_id: uuid.UUID
    clip_id: uuid.UUID
    zone_id: uuid.UUID
    subject_id: uuid.UUID
    record_id: uuid.UUID
    sha256: str
    size_bytes: int
    duration_ms: int | None
    media_kind: str
    path: str | None
    """La ruta dentro del paquete en la forma con clips; ``None`` en la forma sin clips."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeclaredAttachment:
    """Un adjunto de cierre subido por una persona bajo declaración firmada (BR-LAZ-98)."""

    attachment_id: uuid.UUID
    subject_id: uuid.UUID
    zone_id: uuid.UUID
    sha256: str
    size_bytes: int
    content_type: str
    path: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingSummary:
    """Lo que el manifiesto cuenta de cada hallazgo del paquete."""

    subject_id: uuid.UUID
    zone_id: uuid.UUID
    lifecycle_state: str
    evidence_flagged: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactEntry:
    path: str
    kind: str
    sha256: str
    size_bytes: int
    volume: int


@dataclass(frozen=True, slots=True, kw_only=True)
class AbsentEntry:
    """Una entrada que el paquete debería llevar y no lleva todavía, con su motivo."""

    kind: str
    reason_es: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointEntry:
    """Un punto de control de U-02 tomado antes de componer (BR-LAZ-91)."""

    kind: str
    plant_id: uuid.UUID | None
    entry_id: uuid.UUID
    sequence: int
    entry_hash: str
    content: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class StateCounts:
    """Conteos por estado de todos los hallazgos del periodo (BR-LAZ-89)."""

    finding_count: int
    open_finding_count: int
    closed_finding_count: int
    unclosable_count: int
    discarded_count: int
    suspended_count: int
    evidence_flagged_count: int
    by_state: Mapping[str, int]


@dataclass(frozen=True, slots=True, kw_only=True)
class ManifestInputs:
    """Todo lo que ``build_manifest`` necesita; nada sale de la hora del sistema."""

    export_id: uuid.UUID
    package_form: str
    organization: OrganizationRef
    plant: PlantRef
    period_from: datetime
    period_to: datetime
    generated_at: datetime
    generated_by: ActorSnapshot
    zones: tuple[ZoneEntry, ...]
    findings: tuple[FindingSummary, ...]
    coverage: tuple[CoverageZoneSummary, ...]
    regression_periods: tuple[RegressionPeriod, ...]
    revoked_gates: tuple[RevokedGate, ...]
    standards_cited: tuple[StandardCited, ...]
    clips: tuple[DeclaredClip, ...]
    attachments: tuple[DeclaredAttachment, ...]
    artifacts: tuple[ArtifactEntry, ...]
    absent: tuple[AbsentEntry, ...]
    checkpoints: tuple[CheckpointEntry, ...]
    checkpoint_public_keys: tuple[Mapping[str, str], ...]
    verifier_version: str = VERIFIER_VERSION_ABSENT


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportManifest:
    """El manifiesto compuesto (DE §3.2); ``to_json`` da su forma canónica."""

    export_id: uuid.UUID
    package_form: str
    package_form_notice_es: str
    organization: OrganizationRef
    plant: PlantRef
    period_from: datetime
    period_to: datetime
    generated_at: datetime
    generated_by: ActorSnapshot
    counts: StateCounts
    zones: tuple[ZoneEntry, ...]
    coverage_summary: tuple[CoverageZoneSummary, ...]
    regression_periods: tuple[RegressionPeriod, ...]
    revoked_gates: tuple[RevokedGate, ...]
    standards_cited: tuple[StandardCited, ...]
    clips: tuple[DeclaredClip, ...]
    attachments: tuple[DeclaredAttachment, ...]
    aggregated_zone_ids: tuple[uuid.UUID, ...]
    artifacts: tuple[ArtifactEntry, ...]
    absent: tuple[AbsentEntry, ...]
    checkpoints: tuple[CheckpointEntry, ...]
    checkpoint_public_keys: tuple[Mapping[str, str], ...]
    verifier_version: str
    coverage_notice_es: str = COVERAGE_NOTICE_ES
    anonymization_statement_es: str = ANONYMIZATION_STATEMENT_ES
    evidence_flagged_notice_es: str = EVIDENCE_FLAGGED_NOTICE_ES
    regression_notice_es: str = REGRESSION_NOTICE_ES
    aggregation_notice_es: str | None = None
    revocation_notice_es: str | None = None
