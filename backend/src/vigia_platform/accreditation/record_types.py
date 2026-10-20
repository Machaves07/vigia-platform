"""Tipos de registro de la acreditación (domain-entities.md de U-04, §5.1 y §5.2).

Cuatro de los diecisiete tipos de U-04; los otros trece están en ``loop.record_types``, que
documenta las reglas comunes (texto libre narrativo frente a instantáneas, ``evidence_paths``
vacías, versiones solo aditivas). Los cuatro son de la cadena de **planta**, en su versión 1, sin
regla de etiqueta:

- ``export_produced``: lo que se entregó, con sus hashes, sus puntos de control previos y su
  forma (``package_form``, nota U04-H-03); sin texto libre;
- ``monthly_package_produced``: el paquete sellado de una zona y un mes, con sus resúmenes,
  métricas, exposición, adopción, muestra auditada y cobertura; sin texto libre: una métrica no
  calculable lo dice con ``status`` y nunca vale cero (BR-LAZ-115);
- ``sample_audit_result``: el resultado de un elemento de la muestra, que nunca cambia la
  clasificación auditada (H-39);
- ``commitments_recorded``: los compromisos de la conversación mensual (1 a 20).

``commitments_recorded`` tiene además una **versión 2** (TASK-347), que solo amplía (BR-NUC-52)
para que la actualización de estado y nota de SUP-U05-28 sea **un registro nuevo** del expediente
y la proyección ``Commitment`` siga siendo derivable de él: un registro lleva un alta
(``commitments``, cada compromiso con ``state`` opcional) **o** una actualización (``updates``:
``commitment_id``, ``state`` y ``closed_note`` de 0 a 500, la nota de cierre de DE §3.3). La
actualización no copia el texto del compromiso: no cambia y no se revalida contra una lista de
vocabulario posterior al alta. La versión 2 declara por eso una ruta de texto libre más,
``/updates[*]/closed_note``, que pasa la política base y los dos validadores de U-04. Ninguna
operación escribe ya la versión 1, pero sigue registrada para presentar los registros históricos.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final, Self

from pydantic import Field, StrictFloat, StrictInt, StrictStr, model_validator
from vigia_contracts.models.common import UUID, Sha256Hex, TechnicalId, Timestamp, UUIDv7
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.ledger.domain.coverage import CoverageState
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.loop.domain.enums import (
    CommitmentState,
    ExposureStatus,
    MetricKey,
    MetricStatus,
    MetricUnit,
    PackageForm,
    SampleAuditOutcome,
)
from vigia_platform.loop.record_types import (
    CalendarDate,
    ReasonCategory,
    UserSnapshot,
    UserSnapshotMaybeLicensed,
)
from vigia_platform.shared.context import ActorUnit

__all__ = [
    "ACCREDITATION_RECORD_TYPES",
    "COMMITMENTS_RECORDED_V2_FREE_TEXT_PATHS",
    "NARRATIVE_FREE_TEXT_PATHS",
    "SNAPSHOT_TEXT_PATHS",
    "register_accreditation_record_types",
]

MAX_EXPORT_ZONES: Final = 20
"""Zonas de una exportación (respuesta 11, ``[estimación propia]``)."""
MAX_COUNT: Final = 10_000_000
MAX_CHECKPOINTS: Final = 1_024
"""Puntos de control de una exportación: uno por cadena cubierta `[estimación propia]`."""
MAX_ARTIFACTS: Final = 10_000_000
MAX_SAMPLE: Final = 30
"""Tamaño máximo de la muestra auditada (§3.3)."""
MAX_STRATA: Final = 256
MAX_COMMITMENTS: Final = 20
"""Compromisos por registro (§5.2)."""
MAX_PREVIOUS_COMMITMENTS: Final = 200
MAX_MINUTES: Final = 31 * 24 * 60 * 20
"""Minutos de un mes completo en 20 zonas: tope de un recuento de minutos."""

Count = Annotated[StrictInt, Field(ge=0, le=MAX_COUNT)]
Minutes = Annotated[StrictFloat, Field(ge=0.0, le=float(MAX_MINUTES))]
Percent = Annotated[StrictFloat, Field(ge=0.0, le=100.0)]
DurationMs = Annotated[StrictInt, Field(ge=0, le=366 * 24 * 3600 * 1000)]

Month = Annotated[
    StrictStr, Field(min_length=7, max_length=7, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
]
"""Mes del paquete, ``AAAA-MM``, en la zona horaria de la planta."""

CommitmentText = Annotated[StrictStr, Field(min_length=20, max_length=1000)]
"""Texto de un compromiso (§3.3)."""
CommentText = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
"""Comentario de la auditoría por muestra (§3.3: 0 a 1000)."""
ClosedNote = Annotated[StrictStr, Field(min_length=1, max_length=500)]
"""Nota de cierre de un compromiso (§3.3: 0 a 500; sin nota, el campo no va)."""


# --- exportación (C-PLA-29) -----------------------------------------------------------------


class Period(ContentModel):
    """El ``{from, to}`` de §5.2: inicio inclusivo y fin exclusivo, en UTC.

    Se llama ``starts_at`` y ``ends_at`` porque ``from`` es palabra reservada de Python y el
    contenido no admite alias (``alias_problems``, P3)."""

    starts_at: Timestamp
    ends_at: Timestamp

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("el periodo termina después de empezar")
        return self


class ExportProduced(ContentModel):
    """Un paquete de exportación entregado: una organización, una planta (H-37, respuesta 11)."""

    export_id: UUIDv7
    zone_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_EXPORT_ZONES)]
    period: Period
    package_form: PackageForm
    finding_count: Count
    open_finding_count: Count
    closed_finding_count: Count
    unclosable_count: Count
    discarded_count: Count
    document_sha256: Sha256Hex
    archive_sha256: Sha256Hex
    manifest_sha256: Sha256Hex
    artifact_count: Annotated[StrictInt, Field(ge=1, le=MAX_ARTIFACTS)]
    checkpoint_record_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_CHECKPOINTS)]
    verifier_version: TechnicalId
    requested_by: UserSnapshot
    generated_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(set(self.zone_ids)) != len(self.zone_ids):
            raise ValueError("zone_ids no admite zonas repetidas")
        return self


# --- paquete mensual (C-PLA-30) -------------------------------------------------------------


class FindingsSummary(ContentModel):
    received: Count
    classified: Count
    confirmed: Count
    discarded: Count
    closed: Count
    overdue: Count
    unclosable: Count
    reopened: Count


class RecurrenceEntry(ContentModel):
    family: PredicateFamily
    episodes_after_closure: Count
    reference_closure_ids: Annotated[tuple[UUIDv7, ...], Field(max_length=200)]


class MetricValue(ContentModel):
    """Una métrica del programa: sin insumo completo, ``not_computable`` y sin valor (§3.4)."""

    key: MetricKey
    status: MetricStatus
    value: Annotated[StrictFloat, Field(ge=0.0, le=float(MAX_COUNT))] | None = None
    numerator: Count | None = None
    denominator: Count | None = None
    unit: MetricUnit
    excluded_unclosable_count: Count

    @model_validator(mode="after")
    def _no_zero_instead_of_nothing(self) -> Self:
        if self.status is MetricStatus.NOT_COMPUTABLE and self.value is not None:
            raise ValueError("una métrica no calculable no lleva valor (nunca cero)")
        return self


class ExposureSummary(ContentModel):
    """El indicador de exposición del mes: lo pendiente de clasificar se declara aparte (§3.5)."""

    status: ExposureStatus
    confirmed_minutes: Minutes | None = None
    pending_classification_minutes: Minutes | None = None
    observable_coverage_percent: Percent | None = None


class AdoptionSummary(ContentModel):
    active_days: Annotated[StrictInt, Field(ge=0, le=31)]
    silent_days: Annotated[StrictInt, Field(ge=0, le=31)]


class SampleStratum(ContentModel):
    family: PredicateFamily
    reason_category: ReasonCategory
    population: Count
    drawn: Annotated[StrictInt, Field(ge=0, le=MAX_SAMPLE)]


class AuditSampleSummary(ContentModel):
    """La muestra auditada con su semilla registrada, para reproducir la selección (§3.3)."""

    sample_id: UUIDv7
    population_size: Count
    sample_size: Annotated[StrictInt, Field(ge=0, le=MAX_SAMPLE)]
    seed: Annotated[StrictInt, Field(ge=0, le=2**64 - 1)]
    strata: Annotated[tuple[SampleStratum, ...], Field(max_length=MAX_STRATA)]


class CommitmentStatus(ContentModel):
    commitment_id: UUIDv7
    state: CommitmentState


class CoverageShare(ContentModel):
    """Tiempo del mes en cada estado de cobertura de U-02: ningún tramo se omite (H-38, P2)."""

    state: CoverageState
    duration_ms: DurationMs


class MonthlyPackageProduced(ContentModel):
    """El paquete mensual sellado de una zona (respuesta 13, BR-LAZ-102)."""

    package_id: UUIDv7
    zone_id: UUID
    period: Month
    findings_summary: FindingsSummary
    recurrence_summary: Annotated[tuple[RecurrenceEntry, ...], Field(max_length=8)]
    metrics: Annotated[tuple[MetricValue, ...], Field(max_length=len(MetricKey))]
    exposure: ExposureSummary
    adoption: AdoptionSummary
    audit_sample: AuditSampleSummary
    commitments_from_previous_period: Annotated[
        tuple[CommitmentStatus, ...], Field(max_length=MAX_PREVIOUS_COMMITMENTS)
    ]
    coverage_summary: Annotated[tuple[CoverageShare, ...], Field(max_length=len(CoverageState))]
    package_sha256: Sha256Hex
    sealed_checkpoint_record_id: UUID
    generated_at: Timestamp


# --- auditoría por muestra y compromisos ----------------------------------------------------


class SampleAuditResult(ContentModel):
    """El resultado de un elemento de la muestra: observa y no cambia nada (H-39)."""

    sample_item_id: UUIDv7
    sample_id: UUIDv7
    package_id: UUIDv7
    classification_id: UUIDv7
    classification_record_id: UUID
    outcome: SampleAuditOutcome
    comment: CommentText | None = None
    reviewed_by: UserSnapshotMaybeLicensed
    reviewed_at: Timestamp


class CommitmentEntry(ContentModel):
    commitment_id: UUIDv7
    text: CommitmentText
    responsible_user_id: UUID
    due_date: CalendarDate


class CommitmentsRecorded(ContentModel):
    """Los compromisos que registra el mando de línea; nada sobre ningún hallazgo (H12, H-31)."""

    commitment_batch_id: UUIDv7
    package_id: UUIDv7
    zone_id: UUID
    commitments: Annotated[
        tuple[CommitmentEntry, ...], Field(min_length=1, max_length=MAX_COMMITMENTS)
    ]
    recorded_by: UserSnapshot
    recorded_at: Timestamp


class CommitmentEntryV2(ContentModel):
    """Un compromiso que se da de alta, con su estado (versión 2, TASK-347; SUP-U05-27)."""

    commitment_id: UUIDv7
    text: CommitmentText
    responsible_user_id: UUID
    due_date: CalendarDate
    state: CommitmentState | None = None
    """Sin estado (los registros de la versión 1), el compromiso nace ``open``."""


class CommitmentUpdate(ContentModel):
    """El cambio de estado y nota de un compromiso ya registrado (versión 2; SUP-U05-28).

    Solo el identificador, el estado y la nota: el texto, el responsable y la fecha no cambian y
    no se vuelven a escribir (ni a validar contra una lista de vocabulario posterior al alta)."""

    commitment_id: UUIDv7
    state: CommitmentState
    closed_note: ClosedNote | None = None
    """Solo con ``done`` o ``dropped`` (lo exige el servicio, no el esquema)."""


class CommitmentsRecordedV2(ContentModel):
    """Versión 2: un alta de compromisos (``commitments``) **o** una actualización
    (``updates``), nunca las dos ni ninguna."""

    commitment_batch_id: UUIDv7
    package_id: UUIDv7
    zone_id: UUID
    commitments: Annotated[tuple[CommitmentEntryV2, ...], Field(max_length=MAX_COMMITMENTS)] = ()
    updates: Annotated[tuple[CommitmentUpdate, ...], Field(max_length=MAX_COMMITMENTS)] = ()
    recorded_by: UserSnapshot
    recorded_at: Timestamp

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if bool(self.commitments) == bool(self.updates):
            raise ValueError("un alta (commitments) o una actualización (updates), solo una")
        return self


# --- registro -------------------------------------------------------------------------------

NARRATIVE_FREE_TEXT_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    "export_produced": (),
    "monthly_package_produced": (),
    "sample_audit_result": ("/comment",),
    "commitments_recorded": ("/commitments[*]/text",),
}
"""La columna «Rutas de texto libre» de §5.1 para los tipos de la acreditación, exactamente."""

SNAPSHOT_TEXT_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    "export_produced": ("/requested_by/display_name",),
    "monthly_package_produced": (),
    "sample_audit_result": (
        "/reviewed_by/display_name",
        "/reviewed_by/professional_license",
    ),
    "commitments_recorded": ("/recorded_by/display_name",),
}
"""Instantáneas del usuario que la metapropiedad de U-02 cuenta como texto libre."""

COMMITMENTS_RECORDED_V2_FREE_TEXT_PATHS: Final[tuple[str, ...]] = ("/updates[*]/closed_note",)
"""La ruta narrativa que añade la versión 2 de ``commitments_recorded`` (TASK-347)."""


def _u04(
    name: str,
    model: type[ContentModel],
    source_key_path: str,
    outbox_events: tuple[str, ...],
    *,
    schema_version: int = 1,
    extra_free_text_paths: tuple[str, ...] = (),
) -> RecordType:
    return RecordType(
        record_type=name,
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=schema_version,
        content_model=model,
        source_key_path=source_key_path,
        free_text_paths=(
            *NARRATIVE_FREE_TEXT_PATHS[name],
            *extra_free_text_paths,
            *SNAPSHOT_TEXT_PATHS[name],
        ),
        outbox_events=outbox_events,
    )


ACCREDITATION_RECORD_TYPES: Final[tuple[RecordType, ...]] = (
    _u04("export_produced", ExportProduced, "/export_id", ("export_available",)),
    _u04(
        "monthly_package_produced",
        MonthlyPackageProduced,
        "/package_id",
        ("monthly_package_available",),
    ),
    _u04("sample_audit_result", SampleAuditResult, "/sample_item_id", ()),
    _u04(
        "commitments_recorded",
        CommitmentsRecorded,
        "/commitment_batch_id",
        ("commitments_recorded",),
    ),
    _u04(
        "commitments_recorded",
        CommitmentsRecordedV2,
        "/commitment_batch_id",
        ("commitments_recorded",),
        schema_version=2,
        extra_free_text_paths=COMMITMENTS_RECORDED_V2_FREE_TEXT_PATHS,
    ),
)
"""Los cuatro tipos de la acreditación, en el orden de §5.1, cada uno con todas sus versiones en
orden: todos en la 1 y ``commitments_recorded`` también en la 2 (TASK-347)."""


def register_accreditation_record_types(registry: RecordTypeRegistry) -> None:
    """Registra los cuatro tipos de la acreditación (con todas sus versiones) al arrancar."""
    for definition in ACCREDITATION_RECORD_TYPES:
        registry.register(definition)
