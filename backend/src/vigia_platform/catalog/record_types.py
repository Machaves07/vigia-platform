"""Tipos de registro del catálogo y las compuertas (domain-entities.md de U-03, §5).

Trece de los veintiséis tipos de U-03; los otros trece (ingesta, alta, credenciales y versiones
de flota) están en ``fleet.record_types``. Todos los escribe U-03, en la cadena de **planta** y en
su versión 1, con un modelo de contenido estricto (``ContentModel``). Retirar un tipo o una
versión está prohibido (BR-NUC-52): una versión nueva solo amplía.

- **Texto libre** (A-45): solo en las rutas declaradas de ``free_text_paths``; pasa la política
  base de U-02 y el validador mínimo de U-03 (D-7). Ningún campo identifica a una persona
  observada (BR-NUC-51): los únicos usuarios son firmantes y responsables de la plataforma.
- **Documentos**: los ``document_ref`` (acta, acuerdo, política y captura del difuminado) son
  objetos propios de U-03, no ``Evidence`` de U-02 (nota de §3.14): no van en
  ``evidence_paths``, que ninguno de estos tipos declara.
- **Clave de idempotencia compuesta** (``zone_id`` + ``catalog_version``, ``standard_id`` +
  ``version``): el escritor lee ``source_key_path`` como una cadena de 1 a 64 caracteres del
  contenido, así que estos tipos llevan ``source_key`` (``<uuid>:<entero>``), que el modelo
  comprueba que coincide con sus partes.
- **Instantes**: van en el contenido cuando el tipo los tiene (``closed_at``, ``marked_at``…);
  ningún tipo depende de ``occurred_at`` del registro, que queda fuera del hash.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from vigia_contracts.models.common import UUID, Sha256Hex, TechnicalId, Timestamp, UUIDv7
from vigia_contracts.models.enumerations import GateStatus, PredicateFamily, ZoneMode
from vigia_contracts.models.zone_catalog import SignedEnvelope as SignedZoneCatalog

from vigia_platform.catalog.domain.enums import (
    AdmissionCriterion,
    AdmissionResult,
    CatalogChangedField,
    ConfirmationOrigin,
    GateKind,
    OcclusionVerification,
    RegressionCause,
    StepKind,
)
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.shared.context import ActorUnit, Role

__all__ = [
    "CATALOG_RECORD_TYPES",
    "MAX_CAMERAS",
    "MAX_MATRIX_ROWS",
    "MAX_SIGNATORIES",
    "DocumentRef",
    "register_catalog_record_types",
]

MAX_CAMERAS: Final = 8
"""Cámaras por zona (``ZoneCatalog``: 1 a 8)."""
MAX_MATRIX_ROWS: Final = 1024
"""Filas de la matriz del walk-test `[estimación propia]`: 32 estándares por sus combinaciones de
condiciones por 4 posturas caben de sobra; con esta cifra una carga de evento con todas las filas
afectadas sigue por debajo de 64 KB (``regression_marked``)."""
MAX_SIGNATORIES: Final = 32
"""Firmantes de un acuerdo o de un acta `[estimación propia]` (el mínimo es 3, H-50)."""
MAX_CORRELATED_EVENTS: Final = 256
"""Eventos de observabilidad correlacionados con una prueba de oclusión `[estimación propia]`."""
MAX_DOCUMENT_BYTES: Final = 20 * 1024 * 1024
"""Documento firmado: hasta 20 MB (NFR-GOB-23)."""
MAX_DURATION_MS: Final = 366 * 24 * 3600 * 1000
"""Tope de una duración en milisegundos (un año): ningún paso ni acta dura más."""
MAX_LATENCY_MS: Final = 3_600_000
MAX_REPETITIONS: Final = 1_000_000
MAX_COUNT: Final = 1_000_000

CatalogVersion = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
StandardVersion = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
Count = Annotated[StrictInt, Field(ge=0, le=MAX_COUNT)]
DurationMs = Annotated[StrictInt, Field(ge=0, le=MAX_DURATION_MS)]
LatencyMs = Annotated[StrictInt, Field(ge=0, le=MAX_LATENCY_MS)]
Repetitions = Annotated[StrictInt, Field(ge=0, le=MAX_REPETITIONS)]
Rate = Annotated[StrictFloat, Field(ge=0.0, le=1_000_000.0)]
Fps = Annotated[StrictFloat, Field(ge=0.0, le=1_000.0)]

ReasonEs = Annotated[StrictStr, Field(min_length=10, max_length=500)]
"""Motivo (``reason_es``, ``declared_reason_es``): texto libre de 10 a 500 `[estimación propia]`."""
ScopeText = Annotated[StrictStr, Field(min_length=1, max_length=4000)]
FramingDescription = Annotated[StrictStr, Field(min_length=1, max_length=500)]
Justification = Annotated[StrictStr, Field(min_length=1, max_length=2000)]
CriteriaSummary = Annotated[StrictStr, Field(min_length=1, max_length=2000)]
DisplayName = Annotated[StrictStr, Field(min_length=1, max_length=120)]
"""Nombre del firmante del cliente (no es usuario de la plataforma ni persona observada)."""
LegalReference = Annotated[StrictStr, Field(min_length=1, max_length=120)]

_UUID_PATTERN: Final = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

VersionedKey = Annotated[
    StrictStr,
    Field(min_length=38, max_length=47, pattern=f"^{_UUID_PATTERN}:[1-9][0-9]{{0,9}}$"),
]
"""``source_key`` compuesta: ``<uuid>:<versión>`` (hasta 47 caracteres; el tope es 64)."""

DocumentStorageKey = Annotated[
    StrictStr,
    Field(
        min_length=134,
        max_length=134,
        pattern=(
            f"^org/{_UUID_PATTERN}/plant/{_UUID_PATTERN}/documents/{_UUID_PATTERN}"
            r"\.(pdf|jpg|png)$"
        ),
    ),
]
"""``org/{organization_id}/plant/{plant_id}/documents/{document_id}.{ext}`` (infrastructure-design
§4.1): 134 caracteres justos con cualquiera de las tres extensiones."""

DocumentContentType = Literal["application/pdf", "image/jpeg", "image/png"]


def _versioned_key(identifier: str, version: int) -> str:
    return f"{identifier}:{version}"


# --- piezas compartidas -------------------------------------------------------------------


class DocumentRef(ContentModel):
    """Referencia a un documento firmado de planta (§3.14): nunca el documento."""

    document_id: UUIDv7
    storage_key: DocumentStorageKey
    sha256: Sha256Hex
    content_type: DocumentContentType
    size_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_DOCUMENT_BYTES)]


# --- catálogo (C-PLA-07) ------------------------------------------------------------------


class CatalogVersionPublished(ContentModel):
    """Una versión del catálogo de una zona con su sobre firmado conservado (BR-NUC-87)."""

    source_key: VersionedKey
    zone_id: UUID
    catalog_version: CatalogVersion
    changed_fields: Annotated[tuple[CatalogChangedField, ...], Field(min_length=1, max_length=8)]
    reason_es: ReasonEs
    envelope: SignedZoneCatalog

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.source_key != _versioned_key(self.zone_id, self.catalog_version):
            raise ValueError("source_key debe ser <zone_id>:<catalog_version>")
        if len(set(self.changed_fields)) != len(self.changed_fields):
            raise ValueError("changed_fields no admite valores repetidos")
        payload = self.envelope.payload
        if payload.zone_id != self.zone_id or payload.version != self.catalog_version:
            raise ValueError("el sobre firmado es de otra zona o de otra versión del catálogo")
        return self


class CatalogStandardRetired(ContentModel):
    """Retiro de un estándar: una versión nueva del catálogo sin él, nunca un borrado."""

    source_key: VersionedKey
    zone_id: UUID
    standard_id: UUID
    version: StandardVersion
    retired_in_catalog_version: CatalogVersion
    reason_es: ReasonEs

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.source_key != _versioned_key(self.standard_id, self.version):
            raise ValueError("source_key debe ser <standard_id>:<version>")
        return self


class SingleOccupancyDeclared(ContentModel):
    """Marca unipersonal de la zona: atributo de plataforma que no viaja al nodo (respuesta 3)."""

    source_key: VersionedKey
    zone_id: UUID
    catalog_version: CatalogVersion
    single_occupancy: StrictBool
    aggregation_window_minutes: Annotated[StrictInt, Field(ge=15, le=480)]
    reason_es: ReasonEs

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.source_key != _versioned_key(self.zone_id, self.catalog_version):
            raise ValueError("source_key debe ser <zone_id>:<catalog_version>")
        return self


# --- admisión (C-PLA-08) ------------------------------------------------------------------


class AdmissionAnswers(ContentModel):
    standard: StrictBool
    remedy: StrictBool
    subject: StrictBool


class StandardAdmissionTest(ContentModel):
    """Prueba de admisión de tres preguntas; se registran las aprobadas y las rechazadas."""

    admission_id: UUIDv7
    plant_id: UUID
    family: PredicateFamily
    answers: AdmissionAnswers
    result: AdmissionResult
    failed_criterion: AdmissionCriterion | None = None
    justification_es: Justification | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        answers = {
            AdmissionCriterion.STANDARD: self.answers.standard,
            AdmissionCriterion.REMEDY: self.answers.remedy,
            AdmissionCriterion.SUBJECT: self.answers.subject,
        }
        admitted = all(answers.values())
        if (self.result is AdmissionResult.ADMITTED) != admitted:
            raise ValueError("result es admitted si y solo si las tres respuestas son afirmativas")
        if admitted and self.failed_criterion is not None:
            raise ValueError("una admisión aprobada no lleva failed_criterion")
        if not admitted and (self.failed_criterion is None or answers[self.failed_criterion]):
            raise ValueError("un rechazo nombra en failed_criterion una respuesta negativa")
        return self


# --- compuertas, actas, acuerdos y política (C-PLA-09) ------------------------------------


class GateStateChanged(ContentModel):
    """Aprobación o revocación de una compuerta de la zona (respuesta 7)."""

    zone_id: UUID
    gate: GateKind
    status: GateStatus
    resulting_mode: ZoneMode
    record_id: UUID | None = None
    agreement_id: UUID | None = None
    reason_es: ReasonEs | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if (self.status is GateStatus.REVOKED) != (self.reason_es is not None):
            raise ValueError("reason_es es obligatorio al revocar y solo al revocar")
        return self


class ScopeCamera(ContentModel):
    camera_id: UUID
    framing_description_es: FramingDescription
    reference_marker: StrictBool


class BlurDeclaration(ContentModel):
    """Parte declarada de la verificación del difuminado (D-2): basta para el montaje."""

    declared_by: UUID
    declared_at: Timestamp
    capture_document_ref: DocumentRef


class MountingGateRecord(ContentModel):
    """Acta de alcance completa (§2.6 con la nota de D-2)."""

    record_id: UUIDv7
    zone_id: UUID
    scope_text_es: ScopeText
    cameras: Annotated[tuple[ScopeCamera, ...], Field(min_length=1, max_length=MAX_CAMERAS)]
    blur_verification: BlurDeclaration
    document_ref: DocumentRef | None = None
    signed_by: UUID
    role_in_use: Role
    plant_policy_loaded_at_signing: StrictBool

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        cameras = [camera.camera_id for camera in self.cameras]
        if len(set(cameras)) != len(cameras):
            raise ValueError("cada cámara aparece una sola vez en el acta")
        return self


class Signatory(ContentModel):
    role: Role
    user_id: UUID


class AgreementConfirmation(ContentModel):
    user_id: UUID
    role_in_use: Role
    confirmed_at: Timestamp
    origin: ConfirmationOrigin


class UseAgreementSigned(ContentModel):
    """Acuerdo de uso aprobado con todas sus confirmaciones en la aplicación (respuesta 5)."""

    agreement_id: UUIDv7
    zone_id: UUID
    signatories: Annotated[tuple[Signatory, ...], Field(min_length=3, max_length=MAX_SIGNATORIES)]
    confirmations: Annotated[
        tuple[AgreementConfirmation, ...], Field(min_length=3, max_length=MAX_SIGNATORIES)
    ]
    document_ref: DocumentRef | None = None
    replaces_agreement_id: UUIDv7 | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        signers = [signatory.user_id for signatory in self.signatories]
        confirmed = [confirmation.user_id for confirmation in self.confirmations]
        if len(set(signers)) != len(signers) or len(set(confirmed)) != len(confirmed):
            raise ValueError("cada firmante aparece y confirma una sola vez")
        if not any(signatory.role is Role.COPASST for signatory in self.signatories):
            raise ValueError("el acuerdo exige la representación de los trabajadores (copasst)")
        if set(confirmed) != set(signers):
            raise ValueError("cada firmante esperado confirma, y solo ellos")
        if self.replaces_agreement_id == self.agreement_id:
            raise ValueError("un acuerdo no se sustituye a sí mismo")
        return self


class PlantPolicySigned(ContentModel):
    """Política de hallazgos incerrables firmada por el cliente (H-26)."""

    policy_id: UUIDv7
    plant_id: UUID
    version: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    signed_at: Timestamp
    signed_by_display_name: DisplayName
    legal_opinion_reference: LegalReference
    criteria_summary_es: CriteriaSummary
    document_ref: DocumentRef


# --- comisionamiento (C-PLA-10) -----------------------------------------------------------


class StepCorrection(ContentModel):
    started_at: Timestamp | None = None
    ended_at: Timestamp | None = None
    reason_es: ReasonEs
    corrected_by: UUID
    corrected_at: Timestamp


class CommissioningStep(ContentModel):
    """Paso cronometrado: el responsable solo vive aquí y en su fila (H-53)."""

    step_id: UUIDv7
    session_id: UUIDv7
    step_kind: StepKind
    responsible_user_id: UUID
    started_at: Timestamp
    ended_at: Timestamp | None = None
    correction: StepCorrection | None = None


class MatrixResult(ContentModel):
    row_id: UUID
    detected: Count
    missed: Count
    false_alarms: Count


class FalseAlarmAcceptance(ContentModel):
    reason_es: ReasonEs
    accepted_by: UUID
    accepted_at: Timestamp


LatencyMeasuredBy = Literal["node", "platform", "browser", "installer"]
"""Reloj de cada tramo (NFR-GOB-70; ``installer`` en comisionamiento, nota de D-2)."""


class LatencyTranche(ContentModel):
    """Un tramo de latencia con su propio reloj; el del instalador solo trae el p95."""

    median_ms: LatencyMs | None = None
    p95_ms: LatencyMs
    max_ms: LatencyMs | None = None
    repetitions: Repetitions
    measured_by: LatencyMeasuredBy


class Latency(ContentModel):
    """Los cuatro tramos de NFR-GOB-70 por separado (errata U03-H-17) y la suma orientativa.

    ``exposure_tranche`` (3a) falta cuando U-05 no envió muestras: el acta lo marca «no medido».
    ``indicative_sum_p95_ms`` es orientativa y nunca se promete como cifra única (P6).
    """

    node_tranche: LatencyTranche | None = None
    platform_tranche: LatencyTranche
    exposure_tranche: LatencyTranche | None = None
    served_tranche: LatencyTranche
    indicative_sum_p95_ms: Annotated[StrictInt, Field(ge=0, le=4 * MAX_LATENCY_MS)]


class CameraMeasured(ContentModel):
    camera_id: UUID
    measured_fps: Fps
    declared_min_fps: Fps


class OcclusionSummary(ContentModel):
    camera_id: UUID
    verification: OcclusionVerification


class StepSummary(ContentModel):
    """Duración total de un tipo de paso, **sin responsable** (H-53)."""

    step_kind: StepKind
    duration_ms: DurationMs


class CommissioningSignature(ContentModel):
    user_id: UUID
    role_in_use: Role
    signed_at: Timestamp


class Baseline(ContentModel):
    """Fecha de la línea base de un par cámara-zona leída del ``LocalStatus`` del nodo."""

    camera_id: UUID
    zone_id: UUID
    captured_at: Timestamp


class InstallerMeasurements(ContentModel):
    """Lo que el instalador lee del ``LocalStatus`` y envía al cerrar (interfaces v1.5, b)."""

    beacon_latency_ms_p95: LatencyMs
    baselines: Annotated[tuple[Baseline, ...], Field(min_length=0, max_length=MAX_CAMERAS)]


class WalkTestResult(ContentModel):
    """Acta digital completa (§2.15 con sus notas fechadas).

    Las horas van en milisegundos enteros (convención de duraciones del proyecto), así que el
    total es la suma **exacta** de los pasos, que el modelo comprueba (H-53, gate 10).
    """

    commissioning_record_id: UUIDv7
    session_id: UUIDv7
    zone_id: UUID
    catalog_version: CatalogVersion
    matrix_results: Annotated[
        tuple[MatrixResult, ...], Field(min_length=1, max_length=MAX_MATRIX_ROWS)
    ]
    false_negatives_total: Count
    false_alarm_rate_observed: Rate
    false_alarm_threshold: Rate
    false_alarm_acceptance: FalseAlarmAcceptance | None = None
    latency: Latency
    cameras_measured: Annotated[
        tuple[CameraMeasured, ...], Field(min_length=1, max_length=MAX_CAMERAS)
    ]
    occlusion_summary: Annotated[
        tuple[OcclusionSummary, ...], Field(min_length=1, max_length=MAX_CAMERAS)
    ]
    total_duration_ms: DurationMs
    steps_summary: Annotated[tuple[StepSummary, ...], Field(min_length=0, max_length=8)]
    installer_measurements: InstallerMeasurements | None = None
    signatures: Annotated[
        tuple[CommissioningSignature, ...], Field(min_length=1, max_length=MAX_SIGNATORIES)
    ]
    closed_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        rows = [result.row_id for result in self.matrix_results]
        if len(set(rows)) != len(rows):
            raise ValueError("una entrada por fila de la matriz")
        if self.false_negatives_total != sum(result.missed for result in self.matrix_results):
            raise ValueError("false_negatives_total es la suma de los missed de la matriz")
        kinds = [step.step_kind for step in self.steps_summary]
        if len(set(kinds)) != len(kinds):
            raise ValueError("steps_summary lleva cada tipo de paso una sola vez")
        if self.total_duration_ms != sum(step.duration_ms for step in self.steps_summary):
            raise ValueError("total_duration_ms es la suma exacta de los pasos")
        for label, cameras in (
            ("cameras_measured", [camera.camera_id for camera in self.cameras_measured]),
            ("occlusion_summary", [entry.camera_id for entry in self.occlusion_summary]),
        ):
            if len(set(cameras)) != len(cameras):
                raise ValueError(f"{label} lleva cada cámara una sola vez")
        return self


class OcclusionTestResult(ContentModel):
    """Prueba de redundancia por oclusión (D-6), con ``pending`` y ``deadline`` (nota de §2.14)."""

    test_id: UUIDv7
    session_id: UUIDv7
    camera_id: UUID
    started_at: Timestamp
    ended_at: Timestamp
    deadline: Timestamp
    verification: OcclusionVerification
    correlated_event_ids: Annotated[
        tuple[UUID, ...], Field(min_length=0, max_length=MAX_CORRELATED_EVENTS)
    ]
    declared_reason_es: ReasonEs | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        declared = self.verification is OcclusionVerification.DECLARED
        if declared != (self.declared_reason_es is not None):
            raise ValueError("declared_reason_es es obligatorio en declared y solo en declared")
        if self.verification is OcclusionVerification.VERIFIED and not self.correlated_event_ids:
            raise ValueError("verified exige los eventos correlacionados de la ventana")
        return self


# --- regresión del walk-test (C-PLA-11) ---------------------------------------------------

AffectedRows = (
    Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_MATRIX_ROWS)] | Literal["all"]
)
"""Filas afectadas de la matriz, o ``all`` si cambió encuadre, cámaras, cobertura o modelo."""


class WalkTestRegressionMarked(ContentModel):
    """Marca de acta no vigente, sin bloqueo operativo (respuesta 13)."""

    zone_id: UUID
    cause: RegressionCause
    catalog_version: CatalogVersion | None = None
    model_version: TechnicalId | None = None
    affected_row_ids: AffectedRows
    marked_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.cause is RegressionCause.CATALOG_CHANGE and self.catalog_version is None:
            raise ValueError("catalog_change nombra la versión del catálogo que la disparó")
        if self.cause is RegressionCause.MODEL_VERSION_CHANGE and self.model_version is None:
            raise ValueError("model_version_change nombra la versión del modelo que la disparó")
        if self.cause is RegressionCause.FRAMING_RECAPTURED and self.affected_row_ids != "all":
            raise ValueError("framing_recaptured afecta a la matriz completa (BR-GOB-52)")
        if isinstance(self.affected_row_ids, tuple) and len(set(self.affected_row_ids)) != len(
            self.affected_row_ids
        ):
            raise ValueError("affected_row_ids no admite filas repetidas")
        return self


class WalkTestRegressionCleared(ContentModel):
    zone_id: UUID
    cleared_by_session_id: UUIDv7
    cleared_at: Timestamp


# --- registro --------------------------------------------------------------------------------


def _catalog(
    record_type: str,
    model: type[ContentModel],
    *,
    source_key_path: str | None = None,
    free_text_paths: tuple[str, ...] = (),
    outbox_events: tuple[str, ...] = (),
) -> RecordType:
    return RecordType(
        record_type=record_type,
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=model,
        source_key_path=source_key_path,
        free_text_paths=free_text_paths,
        outbox_events=outbox_events,
    )


ENVELOPE_FREE_TEXT_PATHS: Final = (
    "/envelope/payload/standards[*]/title_es",
    "/envelope/payload/standards[*]/declared_text",
    "/envelope/payload/standards[*]/declared_by/display_name",
    "/envelope/payload/signals[*]/description_es",
    "/envelope/key_id",
)
"""Rutas del ``ZoneCatalog`` firmado que el registro cuenta como texto libre: el título y el texto
declarado del estándar (el único texto libre del contrato, RF-CTR-03), el nombre de quien lo
declaró, la descripción de las señales y el ``key_id`` del sobre, que mezcla mayúsculas y
minúsculas (``schema_rules.is_free_text``)."""

CATALOG_RECORD_TYPES: Final[tuple[RecordType, ...]] = (
    _catalog(
        "catalog_version_published",
        CatalogVersionPublished,
        source_key_path="/source_key",
        free_text_paths=("/reason_es", *ENVELOPE_FREE_TEXT_PATHS),
        outbox_events=("catalog_updated", "regression_marked"),
    ),
    _catalog(
        "standard_admission_test",
        StandardAdmissionTest,
        source_key_path="/admission_id",
        free_text_paths=("/justification_es",),
    ),
    _catalog(
        "gate_state_changed",
        GateStateChanged,
        free_text_paths=("/reason_es",),
        outbox_events=("gate_state_changed", "zone_activated"),
    ),
    _catalog(
        "mounting_gate_record",
        MountingGateRecord,
        source_key_path="/record_id",
        free_text_paths=("/scope_text_es", "/cameras[*]/framing_description_es"),
        outbox_events=("gate_state_changed",),
    ),
    _catalog(
        "use_agreement_signed",
        UseAgreementSigned,
        source_key_path="/agreement_id",
        outbox_events=("gate_state_changed", "zone_activated"),
    ),
    _catalog(
        "commissioning_step",
        CommissioningStep,
        source_key_path="/step_id",
        free_text_paths=("/correction/reason_es",),
    ),
    _catalog(
        "walk_test_result",
        WalkTestResult,
        source_key_path="/commissioning_record_id",
        free_text_paths=("/false_alarm_acceptance/reason_es",),
        outbox_events=("regression_cleared",),
    ),
    _catalog(
        "plant_policy_signed",
        PlantPolicySigned,
        source_key_path="/policy_id",
        free_text_paths=(
            "/signed_by_display_name",
            "/legal_opinion_reference",
            "/criteria_summary_es",
        ),
    ),
    _catalog(
        "occlusion_test_result",
        OcclusionTestResult,
        source_key_path="/test_id",
        free_text_paths=("/declared_reason_es",),
    ),
    _catalog(
        "walk_test_regression_marked",
        WalkTestRegressionMarked,
        outbox_events=("regression_marked",),
    ),
    _catalog(
        "walk_test_regression_cleared",
        WalkTestRegressionCleared,
        outbox_events=("regression_cleared",),
    ),
    _catalog(
        "catalog_standard_retired",
        CatalogStandardRetired,
        source_key_path="/source_key",
        free_text_paths=("/reason_es",),
        outbox_events=("catalog_updated",),
    ),
    _catalog(
        "single_occupancy_declared",
        SingleOccupancyDeclared,
        source_key_path="/source_key",
        free_text_paths=("/reason_es",),
        outbox_events=("catalog_updated",),
    ),
)
"""Los trece tipos del catálogo, en versión 1 (domain-entities §5)."""


def register_catalog_record_types(registry: RecordTypeRegistry) -> None:
    """Registra los trece tipos del catálogo; ``RecordTypeRejected`` si alguno no cumple."""
    for definition in CATALOG_RECORD_TYPES:
        registry.register(definition)
