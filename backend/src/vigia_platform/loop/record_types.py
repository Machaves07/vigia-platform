"""Tipos de registro del lazo (domain-entities.md de U-04, §5.1, §5.2 y §5.3).

Trece de los diecisiete tipos de U-04 del diseño; los otros cuatro (exportación, paquete mensual,
auditoría por muestra y compromisos) están en ``accreditation.record_types``. El decimoctavo,
``closure_attachment_unrecoverable`` (VIG-274), declara un adjunto de cierre perdido sin versión
anterior (DR-LAZ-04, runbook 7.3, paso 4). Todos los escribe U-04 en su
versión 1, con un modelo de contenido estricto (``ContentModel``), en la cadena de **planta**
salvo ``vocabulary_list_changed``, que es de **organización** (la lista es por organización,
respuesta 10). Ninguno declara ``evidence_paths``: los clips ya los registró y verificó U-03 en
``finding_received`` y aquí solo se referencian; la evidencia de cierre no es un clip (§6.3).
Retirar un tipo o una versión está prohibido (BR-NUC-52): una versión nueva solo amplía.

**Texto libre** (BR-NUC-51). ``free_text_paths`` de cada tipo es la unión de dos listas cerradas:

- ``NARRATIVE_FREE_TEXT_PATHS``: la columna «Rutas de texto libre» de §5.1, **exactamente**: lo
  que una persona escribe sobre el hallazgo, la acción o el análisis. Pasa la política base de
  U-02 y los dos validadores de U-04 (``blocked_vocabulary`` y ``person_attribution``, TASK-303
  y TASK-340) antes del hash;
- ``SNAPSHOT_TEXT_PATHS``: las cadenas que la metapropiedad de privacidad de U-02 (PR-NUC-16)
  también cuenta como texto libre aunque §5.1 no las liste, porque no son narración: la
  instantánea del usuario de la plataforma que firma, asigna o revisa (``display_name``,
  ``professional_license``), el cargo de quien ejecutó la acción (``performed_by_role``), el
  nombre del archivo adjunto y los términos de la lista de vocabulario. Ninguna identifica a una
  persona observada (P3): son usuarios de la plataforma, un cargo o un término.

``VALIDATOR_EXEMPT_PATHS`` es la **exclusión declarada** de §2.12 y BR-LAZ-64: el valor de un
término bloqueado (``/added[*]/value`` y ``/removed[*]/value`` de ``vocabulary_list_changed``) se
valida solo con el esquema estricto y la política base de U-02, nunca con el validador de
vocabulario, que rechazaría el término que se quiere prohibir.

La versión 2 de ``vocabulary_list_changed`` (TASK-350) añade los nombres del vocabulario
declarado (``VOCABULARY_LIST_CHANGED_V2_NAME_PATHS``): pasan los dos validadores, como un nombre de
zona, y no son exentas.

**Etiquetas** (§5.3): solo ``classification`` y ``review_resolution`` proyectan una ``Label`` de
U-02, con ``subject_record_id`` en ``/anchor_record_id`` y el firmante en ``/signer`` y
``/resolved_by``; una etiqueta nunca lleva texto libre.

Las longitudes de los textos son las de §2 (motivos de 20 a 1000 caracteres, descripciones de 20
a 2000, notas de hasta 1000); los topes de las listas sin cifra en el diseño son
``[estimación propia]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from vigia_contracts.models.common import UUID, Sha256Hex, Timestamp, UUIDv7
from vigia_contracts.models.enumerations import ObservabilityState, PredicateFamily

from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    LabelRule,
    RecordType,
    RecordTypeRegistry,
)
from vigia_platform.loop.domain.enums import (
    ActionType,
    AttachmentContentType,
    BlockedTermCategory,
    BlockedTermForm,
    CauseCategory,
    ClassificationOutcome,
    DeclaredNameKind,
    EffectOnLoop,
    EscalationLevel,
    EvidenceRefKind,
    FindingLifecycleState,
    ReasonCategoryAuthorized,
    ReasonCategoryConfirmed,
    ReasonCategoryFalsePositive,
    ReviewResolutionOutcome,
    SubjectKind,
    UnclosableReviewOutcome,
)
from vigia_platform.shared.context import ActorUnit, Role

__all__ = [
    "LOOP_RECORD_TYPES",
    "NARRATIVE_FREE_TEXT_PATHS",
    "SNAPSHOT_TEXT_PATHS",
    "VALIDATOR_EXEMPT_PATHS",
    "VOCABULARY_LIST_CHANGED_V2_NAME_PATHS",
    "BusinessCalendar",
    "CalendarDate",
    "DisplayName",
    "LicensedSigner",
    "ProfessionalLicense",
    "ReasonCategory",
    "StandardRef",
    "UserSnapshot",
    "register_loop_record_types",
]

MAX_COUNT: Final = 10_000_000
"""Tope de un recuento: el orden de 10 millones de hallazgos al año (DE «Escala»)."""
MAX_DAYS: Final = 3_660
MAX_DELAY_MS: Final = 366 * 24 * 3600 * 1000
"""Retraso máximo de un tramo tardío: un año, en milisegundos `[estimación propia]`."""
MAX_ATTACHMENTS_PER_ENTRY: Final = 5
"""Adjuntos por entrada de evidencia (§6.3 y su nota de NFR Requirements)."""
MAX_ATTACHMENTS_PER_FINDING: Final = 20
"""Adjuntos por hallazgo (nota de NFR Requirements de §6.3)."""
MAX_ATTACHMENT_BYTES: Final = 10 * 1024 * 1024
"""10 MB por adjunto (§6.3)."""
MAX_EVIDENCE_REFS: Final = 32
"""Referencias de evidencia de un paso o factor del análisis `[estimación propia]`."""
MAX_REFERENCED_CLIPS: Final = 256
"""Clips del episodio citados en un cierre `[estimación propia]`."""
MAX_TERMS_PER_CHANGE: Final = 200
"""Términos añadidos o retirados en un cambio de la lista de vocabulario `[estimación propia]`."""
MAX_NON_WORKING_DATES: Final = 400
"""Festivos declarados del calendario laboral: 40 por año (§2.13) durante diez años."""

Count = Annotated[StrictInt, Field(ge=0, le=MAX_COUNT)]
Days = Annotated[StrictInt, Field(ge=0, le=MAX_DAYS)]
Sequence32 = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
StandardVersion = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
PolicyVersion = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
"""Versión de la política de planta de U-03 (``plant_policy_signed.version``)."""
Confidence = Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
"""Confianza o umbral de la banda de revisión (U-01 §2.2), de 0 a 1."""

CalendarDate = Annotated[
    StrictStr,
    Field(
        min_length=10,
        max_length=10,
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$",
    ),
]
"""Fecha de calendario ``AAAA-MM-DD`` en la zona horaria de la planta."""

# --- textos libres narrativos (§2) ----------------------------------------------------------

ReasonText = Annotated[StrictStr, Field(min_length=20, max_length=1000)]
"""Motivo de una clasificación, una resolución de cola o una reapertura (§2.3, §2.4)."""
LongText = Annotated[StrictStr, Field(min_length=20, max_length=2000)]
"""Descripción de una acción, un cierre o un incerrable; enunciado del análisis (§2.5 a §2.9)."""
NoteText = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
"""Nota opcional (``verification_note``, ``expected_outcome``, ``missing_information``)."""
StepStatement = Annotated[StrictStr, Field(min_length=20, max_length=1000)]
"""Enunciado de un paso de los cinco porqués (§2.9)."""
FactorText = Annotated[StrictStr, Field(min_length=5, max_length=500)]
"""Factor de una rama del diagrama de causa y efecto (§2.9)."""
ChangeReason = Annotated[StrictStr, Field(min_length=10, max_length=500)]
"""Motivo de un cambio de la lista de vocabulario o de la configuración (§2.12, §2.13)."""

# --- textos de instantánea (no narrativos) --------------------------------------------------

DisplayName = Annotated[StrictStr, Field(min_length=1, max_length=120)]
"""Nombre del usuario de la plataforma en el instante (instantánea): nunca una persona
observada."""
ProfessionalLicense = Annotated[StrictStr, Field(min_length=1, max_length=64)]
"""Número de licencia profesional del firmante (§2.6: texto ≤ 64, obligatorio al firmar)."""
JobTitle = Annotated[StrictStr, Field(min_length=1, max_length=64)]
"""Cargo de quien ejecutó la acción (§2.6): un cargo, jamás un nombre."""
Filename = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[^/\\]+$")]
"""Nombre del archivo adjunto, sin ruta (§2.6)."""
TermValue = Annotated[StrictStr, Field(min_length=1, max_length=256)]
"""Término bloqueado: hasta 64 caracteres un lema y 256 un patrón (§2.12)."""
TermSuggestion = Annotated[StrictStr, Field(min_length=1, max_length=200)]
"""Alternativa que se propone al autor (§2.12)."""
DeclaredNameValue = Annotated[StrictStr, Field(min_length=2, max_length=120)]
"""Nombre del vocabulario declarado: una planta, una zona, un equipo, una máquina o una norma
(TASK-350), con el mismo tope que el nombre de una zona `[estimación propia]`."""

ModelIdentifier = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
]
"""Identificador del modelo del asistente que produjo el borrador (nota de §2.11)."""

type ReasonCategory = (
    ReasonCategoryConfirmed | ReasonCategoryAuthorized | ReasonCategoryFalsePositive
)
"""``reason_category``: de la lista de la salida (§2.3)."""

_REASONS_BY_OUTCOME: Final[Mapping[ClassificationOutcome, frozenset[str]]] = {
    ClassificationOutcome.CONFIRMED: frozenset(ReasonCategoryConfirmed),
    ClassificationOutcome.AUTHORIZED_OPERATION: frozenset(ReasonCategoryAuthorized),
    ClassificationOutcome.FALSE_POSITIVE: frozenset(ReasonCategoryFalsePositive),
}
"""La lista de motivos de cada salida de la clasificación (§1: una lista por salida)."""


# --- piezas compartidas ---------------------------------------------------------------------


class StandardRef(ContentModel):
    """El estándar citado y la versión con la que se evaluó (H-16)."""

    standard_id: UUID
    version: StandardVersion


class UserSnapshot(ContentModel):
    """Instantánea del actor: usuario de la plataforma, su nombre y el rol en uso."""

    user_id: UUID
    display_name: DisplayName
    role_in_use: Role


class UserSnapshotMaybeLicensed(ContentModel):
    """Instantánea del actor con su licencia profesional cuando la tiene (§5.2)."""

    user_id: UUID
    display_name: DisplayName
    role_in_use: Role
    professional_license: ProfessionalLicense | None = None


class LicensedSigner(ContentModel):
    """Firmante con nombre y licencia profesional obligatoria (H-25, RF-PLA-02)."""

    user_id: UUID
    display_name: DisplayName
    professional_license: ProfessionalLicense


class PolicyRef(ContentModel):
    """La política de incerrables de U-03 vigente al declarar (§2.7); U-04 no la interpreta."""

    policy_id: UUID
    version: PolicyVersion
    signed_at: Timestamp
    document_sha256: Sha256Hex


class ReviewedPolicyRef(ContentModel):
    """La política vigente al revisar una declaración (§5.2 ``unclosable_review``)."""

    policy_id: UUID
    version: PolicyVersion
    document_sha256: Sha256Hex


# --- clasificación y cola (C-PLA-22, C-PLA-23) ---------------------------------------------


class Classification(ContentModel):
    """Decisión humana sobre un hallazgo; una reclasificación es otro registro (H-22, P4)."""

    classification_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    anchor_record_id: UUID
    sequence_in_subject: Sequence32
    outcome: ClassificationOutcome
    reason_category: ReasonCategory
    reason_text: ReasonText
    is_reclassification: StrictBool
    reinforced_reason: StrictBool
    previous_outcome: ClassificationOutcome | None = None
    previous_classification_record_id: UUID | None = None
    effect_on_loop: EffectOnLoop
    standard: StandardRef
    family: PredicateFamily
    observability_state_at_event: ObservabilityState
    zone_id: UUID
    signer: UserSnapshot
    classified_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.reason_category.value not in _REASONS_BY_OUTCOME[self.outcome]:
            raise ValueError("reason_category no es de la lista de la salida elegida")
        if self.is_reclassification != (self.sequence_in_subject > 1):
            raise ValueError("is_reclassification exige sequence_in_subject mayor que 1")
        return self


class ReviewResolution(ContentModel):
    """Resolución humana de un elemento de la cola de revisión (RF-PLA-03, H-23)."""

    resolution_id: UUIDv7
    detection_id: UUID
    anchor_record_id: UUID
    outcome: ReviewResolutionOutcome
    reason_category: ReasonCategory
    reason_text: ReasonText
    max_confidence: Confidence
    review_threshold: Confidence
    publication_threshold: Confidence
    review_reason: Literal["low_confidence", "ambiguous", "no_bounding_box", "other"]
    family: PredicateFamily
    standard: StandardRef
    zone_id: UUID
    resolved_by: UserSnapshot
    resolved_at: Timestamp


# --- acciones, evidencia y cierre (C-PLA-24) ------------------------------------------------


class ActionAssigned(ContentModel):
    """Acción correctiva de ingeniería o de proceso con responsable y plazo congelado (H-24)."""

    action_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    anchor_record_id: UUID
    action_type: ActionType
    action_description: LongText
    expected_outcome: NoteText | None = None
    assignee_user_id: UUID
    assignee_display_name: DisplayName
    due_at: Timestamp
    deadline_days: Annotated[StrictInt, Field(ge=1, le=180)]
    root_cause_analysis_record_id: UUID | None = None
    supersedes_action_id: UUIDv7 | None = None
    assigned_by: UserSnapshotMaybeLicensed
    assigned_at: Timestamp


class ClosureAttachment(ContentModel):
    """Adjunto de evidencia de cierre, referenciado por hash; nunca un clip (§6.3)."""

    attachment_id: UUIDv7
    sha256: Sha256Hex
    content_type: AttachmentContentType
    size_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_ATTACHMENT_BYTES)]
    filename: Filename
    no_identifiable_person_declared: Literal[True]


class ActionEvidenceAdded(ContentModel):
    """Descripción estructurada de lo ejecutado, con adjuntos opcionales (H-25, §6.3)."""

    evidence_entry_id: UUIDv7
    action_id: UUIDv7
    subject_id: UUID
    description: LongText
    verification_note: NoteText | None = None
    performed_at: Timestamp
    performed_by_role: JobTitle
    attachments: Annotated[
        tuple[ClosureAttachment, ...], Field(max_length=MAX_ATTACHMENTS_PER_ENTRY)
    ]
    added_by: UserSnapshot
    added_at: Timestamp


class Closure(ContentModel):
    """Cierre firmado con acción y evidencia: sin evidencia ni licencia no existe (H-25, P5)."""

    closure_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    anchor_record_id: UUID
    action_id: UUIDv7
    action_type: ActionType
    closure_description: LongText
    verification_note: NoteText | None = None
    evidence_entry_ids: Annotated[tuple[UUIDv7, ...], Field(min_length=1, max_length=20)]
    attachment_digests: Annotated[
        tuple[Sha256Hex, ...], Field(max_length=MAX_ATTACHMENTS_PER_FINDING)
    ]
    referenced_clip_ids: Annotated[tuple[UUID, ...], Field(max_length=MAX_REFERENCED_CLIPS)]
    signer: LicensedSigner
    signed_at: Timestamp


# --- incerrable y reapertura ----------------------------------------------------------------


class UnclosableStateDeclared(ContentModel):
    """SIN ALTERNATIVA SEGURA DISPONIBLE: los cinco campos a la vez (H13, H-26, respuesta 7)."""

    declaration_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    anchor_record_id: UUID
    evaluated_alternative: LongText
    rejection_reason: LongText
    responsible_user_id: UUID
    responsible_display_name: DisplayName
    decision_date: CalendarDate
    compensatory_measure: LongText
    review_due_at: Timestamp
    policy: PolicyRef
    signer: LicensedSigner
    declared_at: Timestamp


class UnclosableReview(ContentModel):
    """Revisión de una declaración: se renueva o se convierte en acción (respuesta 7)."""

    review_id: UUIDv7
    declaration_id: UUIDv7
    subject_id: UUID
    outcome: UnclosableReviewOutcome
    review_note: LongText
    new_review_due_at: Timestamp | None = None
    new_action_id: UUIDv7 | None = None
    policy: ReviewedPolicyRef
    reviewer: LicensedSigner
    reviewed_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        renewed = self.outcome is UnclosableReviewOutcome.RENEWED
        if renewed != (self.new_review_due_at is not None):
            raise ValueError("new_review_due_at va solo, y siempre, con renewed")
        if (not renewed) != (self.new_action_id is not None):
            raise ValueError("new_action_id va solo, y siempre, con converted_to_action")
        return self


class Reopening(ContentModel):
    """Reapertura de un cerrado o un incerrable: el registro anterior queda íntegro (P4)."""

    reopening_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    from_state: Literal[FindingLifecycleState.CLOSED, FindingLifecycleState.UNCLOSABLE]
    previous_closure_id: UUIDv7 | None = None
    previous_declaration_id: UUIDv7 | None = None
    reason_text: LongText
    new_action_id: UUIDv7
    reopened_by: UserSnapshot
    reopened_at: Timestamp


class ActionOverdue(ContentModel):
    """Un plazo vencido, por nivel de escalamiento: declara un hecho, no cierra ni oculta (P5).

    ``overdue_id`` es el UUID v5 determinista de ``(action_id, due_at, escalation_level)``
    (nota U04-H-07): la tarea periódica es idempotente por construcción."""

    overdue_id: UUID
    action_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    due_at: Timestamp
    declared_at: Timestamp
    days_overdue: Days
    escalation_level: EscalationLevel
    assignee_user_id: UUID
    assignee_inactive: StrictBool


# --- causa raíz (C-PLA-25) ------------------------------------------------------------------


class EvidenceRef(ContentModel):
    """Referencia a un hecho que sostiene un paso o un factor: nunca su contenido (§2.9)."""

    kind: EvidenceRefKind
    id: UUID


class WhyStep(ContentModel):
    """Uno de los cinco porqués; un paso no se salta (§2.9)."""

    step_index: Annotated[StrictInt, Field(ge=1, le=5)]
    statement: StepStatement
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(max_length=MAX_EVIDENCE_REFS)]
    category: CauseCategory | None = None


class CauseEffectFactor(ContentModel):
    text: FactorText
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(max_length=MAX_EVIDENCE_REFS)]


class CauseEffectBranch(ContentModel):
    """Rama del diagrama: proceso, ingeniería u organización; nunca una persona (H-28, P7)."""

    branch_category: CauseCategory
    factors: Annotated[tuple[CauseEffectFactor, ...], Field(min_length=1, max_length=16)]


class AssistantUse(ContentModel):
    """Si hubo asistente, con qué modelo (el que respondió) y si el texto se editó (P8)."""

    used: StrictBool
    model_name: ModelIdentifier | None = None
    model_version: ModelIdentifier | None = None
    text_edited: StrictBool | None = None
    request_id: UUIDv7 | None = None


class RootCauseAnalysis(ContentModel):
    """Análisis firmado (o pendiente de información) con el texto que el humano aceptó (H-28)."""

    analysis_id: UUIDv7
    subject_kind: SubjectKind
    subject_id: UUID
    anchor_record_id: UUID
    why_steps: Annotated[tuple[WhyStep, ...], Field(min_length=1, max_length=5)]
    cause_effect_branches: Annotated[tuple[CauseEffectBranch, ...], Field(max_length=6)]
    root_cause_category: CauseCategory
    root_cause_statement: LongText
    conclusion: LongText
    missing_information: NoteText | None = None
    state: Literal["pending_information", "signed"]
    assistant: AssistantUse
    signer: LicensedSigner
    signed_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        indexes = [step.step_index for step in self.why_steps]
        if indexes != list(range(1, len(indexes) + 1)):
            raise ValueError("los porqués van numerados desde 1, en orden y sin saltos")
        if self.state == "pending_information" and self.missing_information is None:
            raise ValueError("pending_information exige missing_information")
        return self


# --- vocabulario y configuración (C-PLA-28, respuesta 4) -----------------------------------


class AddedTerm(ContentModel):
    term_id: UUIDv7
    category: BlockedTermCategory
    form: BlockedTermForm
    value: TermValue
    suggestion_es: TermSuggestion


class RemovedTerm(ContentModel):
    term_id: UUIDv7
    value: TermValue


class VocabularyListChanged(ContentModel):
    """Una versión nueva, monótona, de la lista de la organización (respuesta 10)."""

    change_id: UUIDv7
    list_version: Sequence32
    added: Annotated[tuple[AddedTerm, ...], Field(max_length=MAX_TERMS_PER_CHANGE)]
    removed: Annotated[tuple[RemovedTerm, ...], Field(max_length=MAX_TERMS_PER_CHANGE)]
    reason: ChangeReason
    changed_by: UserSnapshot
    changed_at: Timestamp


class AddedDeclaredName(ContentModel):
    name_id: UUIDv7
    kind: DeclaredNameKind
    value: DeclaredNameValue


class RemovedDeclaredName(ContentModel):
    name_id: UUIDv7
    value: DeclaredNameValue


class VocabularyListChangedV2(VocabularyListChanged):
    """Versión 2 (TASK-350): además, los nombres del vocabulario declarado que se añaden y se
    retiran (BR-LAZ-84). Solo amplía la 1: los dos campos nuevos son opcionales."""

    declared_names_added: Annotated[
        tuple[AddedDeclaredName, ...], Field(max_length=MAX_TERMS_PER_CHANGE)
    ] = ()
    declared_names_removed: Annotated[
        tuple[RemovedDeclaredName, ...], Field(max_length=MAX_TERMS_PER_CHANGE)
    ] = ()


class BusinessCalendar(ContentModel):
    """Calendario laboral declarado por la planta (§2.13, ``[hipótesis]``)."""

    working_weekdays: Annotated[
        tuple[Annotated[StrictInt, Field(ge=1, le=7)], ...], Field(min_length=1, max_length=7)
    ]
    non_working_dates: Annotated[tuple[CalendarDate, ...], Field(max_length=MAX_NON_WORKING_DATES)]


class LoopSetting(ContentModel):
    """Un parámetro de la configuración del lazo con su valor (un mapa, como lista de pares)."""

    parameter: Annotated[
        StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    ]
    integer_value: Annotated[StrictInt, Field(ge=0, le=100_000)] | None = None
    business_calendar: BusinessCalendar | None = None

    @model_validator(mode="after")
    def _one_value(self) -> Self:
        if (self.integer_value is None) == (self.business_calendar is None):
            raise ValueError("cada parámetro lleva exactamente un valor")
        return self


class LoopConfigurationChanged(ContentModel):
    """Un cambio de configuración con motivo: nunca altera lo ya asignado (respuesta 4)."""

    change_id: UUIDv7
    plant_id: UUID
    version: Sequence32
    previous_values: Annotated[tuple[LoopSetting, ...], Field(max_length=32)]
    new_values: Annotated[tuple[LoopSetting, ...], Field(min_length=1, max_length=32)]
    reason: ChangeReason
    changed_by: UserSnapshot
    changed_at: Timestamp
    applies_to_new_items_only: Literal[True]


class ClosureAttachmentUnrecoverable(ContentModel):
    """Un adjunto de cierre perdido del depósito **sin versión anterior** (DR-LAZ-04, runbook 7.3,
    paso 4; VIG-274): consta como no recuperable con su hash y su fecha.

    No reescribe nada (P4, P5): la entrada de evidencia y el cierre siguen como están, y el cierre
    sigue siendo válido con su descripción estructurada. ``declaration_id`` es el UUID v5 del
    adjunto, así que un adjunto se declara una sola vez y repetir la orden con los mismos datos es
    un duplicado aceptado."""

    declaration_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID
    action_id: UUIDv7
    evidence_entry_id: UUIDv7
    evidence_record_id: UUID
    attachment_id: UUIDv7
    sha256: Sha256Hex
    size_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_ATTACHMENT_BYTES)]
    content_type: AttachmentContentType
    detected_at: Timestamp
    """Cuándo se comprobó la pérdida sin versión anterior (la declara el operador)."""
    previous_version_available: Literal[False]


class LateSegmentAttached(ContentModel):
    """Un tramo que llega tarde se adjunta al episodio; nunca reabre el lazo (§5.2)."""

    segment_record_id: UUID
    episode_id: UUID
    segment_index: Annotated[StrictInt, Field(ge=0, le=2**31 - 1)]
    previous_segment_record_id: UUID | None = None
    received_at: Timestamp
    episode_state_at_arrival: FindingLifecycleState
    reopened_loop: Literal[False]
    delay_ms: Annotated[StrictInt, Field(ge=0, le=MAX_DELAY_MS)]


# --- registro -------------------------------------------------------------------------------

NARRATIVE_FREE_TEXT_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    "classification": ("/reason_text",),
    "review_resolution": ("/reason_text",),
    "action_assigned": ("/action_description", "/expected_outcome"),
    "action_evidence_added": ("/description", "/verification_note"),
    "closure": ("/closure_description", "/verification_note"),
    "unclosable_state_declared": (
        "/evaluated_alternative",
        "/rejection_reason",
        "/compensatory_measure",
    ),
    "root_cause_analysis": (
        "/why_steps[*]/statement",
        "/cause_effect_branches[*]/factors[*]/text",
        "/root_cause_statement",
        "/conclusion",
        "/missing_information",
    ),
    "vocabulary_list_changed": ("/reason",),
    "action_overdue": (),
    "unclosable_review": ("/review_note",),
    "reopening": ("/reason_text",),
    "loop_configuration_changed": ("/reason",),
    "late_segment_attached": (),
    "closure_attachment_unrecoverable": (),
}
"""La columna «Rutas de texto libre» de §5.1 para los tipos del lazo, exactamente (más
``closure_attachment_unrecoverable``, VIG-274, sin texto libre)."""

SNAPSHOT_TEXT_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    "classification": ("/signer/display_name",),
    "review_resolution": ("/resolved_by/display_name",),
    "action_assigned": (
        "/assignee_display_name",
        "/assigned_by/display_name",
        "/assigned_by/professional_license",
    ),
    "action_evidence_added": (
        "/performed_by_role",
        "/attachments[*]/filename",
        "/added_by/display_name",
    ),
    "closure": ("/signer/display_name", "/signer/professional_license"),
    "unclosable_state_declared": (
        "/responsible_display_name",
        "/signer/display_name",
        "/signer/professional_license",
    ),
    "root_cause_analysis": ("/signer/display_name", "/signer/professional_license"),
    "vocabulary_list_changed": (
        "/added[*]/value",
        "/added[*]/suggestion_es",
        "/removed[*]/value",
        "/changed_by/display_name",
    ),
    "action_overdue": (),
    "unclosable_review": ("/reviewer/display_name", "/reviewer/professional_license"),
    "reopening": ("/reopened_by/display_name",),
    "loop_configuration_changed": ("/changed_by/display_name",),
    "late_segment_attached": (),
    "closure_attachment_unrecoverable": (),
}
"""Cadenas de instantánea y de términos que la metapropiedad de U-02 cuenta como texto libre."""

VALIDATOR_EXEMPT_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    "vocabulary_list_changed": ("/added[*]/value", "/removed[*]/value"),
}
"""Exclusión declarada de los validadores de U-04 (§2.12, BR-LAZ-64): solo la política base."""

VOCABULARY_LIST_CHANGED_V2_NAME_PATHS: Final[tuple[str, ...]] = (
    "/declared_names_added[*]/value",
    "/declared_names_removed[*]/value",
)
"""Las cadenas que añade la versión 2 de ``vocabulary_list_changed`` (TASK-350): nombres de
plantas, zonas, equipos, máquinas o normas. Pasan la política base y **los dos validadores**,
como un nombre de zona: ``person_attribution`` las trata como campos de nombre (sin el patrón de
nombre propio, ``attribution.field_kind``), el resto de patrones sí; no son exentas."""

_PLANT: Final = ChainLevel.PLANT
_ORGANIZATION: Final = ChainLevel.ORGANIZATION

_CLASSIFICATION_LABEL: Final = LabelRule(
    subject_record_path="/anchor_record_id",
    family_path="/family",
    outcome_path="/outcome",
    reason_category_path="/reason_category",
    labeled_by_path="/signer",
)
_REVIEW_LABEL: Final = LabelRule(
    subject_record_path="/anchor_record_id",
    family_path="/family",
    outcome_path="/outcome",
    reason_category_path="/reason_category",
    labeled_by_path="/resolved_by",
)


def _u04(
    name: str,
    model: type[ContentModel],
    source_key_path: str,
    outbox_events: tuple[str, ...],
    *,
    chain_level: ChainLevel = _PLANT,
    label_rule: LabelRule | None = None,
    schema_version: int = 1,
    extra_free_text_paths: tuple[str, ...] = (),
) -> RecordType:
    return RecordType(
        record_type=name,
        writer_unit=ActorUnit.U04,
        chain_level=chain_level,
        schema_version=schema_version,
        content_model=model,
        source_key_path=source_key_path,
        free_text_paths=(
            *NARRATIVE_FREE_TEXT_PATHS[name],
            *SNAPSHOT_TEXT_PATHS[name],
            *extra_free_text_paths,
        ),
        label_rule=label_rule,
        outbox_events=outbox_events,
    )


LOOP_RECORD_TYPES: Final[tuple[RecordType, ...]] = (
    _u04(
        "classification",
        Classification,
        "/classification_id",
        (
            "finding_confirmed",
            "finding_discarded",
            "finding_reclassified",
            "loop_suspended",
            "loop_resumed",
        ),
        label_rule=_CLASSIFICATION_LABEL,
    ),
    _u04(
        "review_resolution",
        ReviewResolution,
        "/resolution_id",
        ("review_confirmed", "review_discarded"),
        label_rule=_REVIEW_LABEL,
    ),
    _u04("action_assigned", ActionAssigned, "/action_id", ("action_assigned",)),
    _u04(
        "action_evidence_added",
        ActionEvidenceAdded,
        "/evidence_entry_id",
        ("action_evidence_added",),
    ),
    _u04("closure", Closure, "/closure_id", ("finding_closed",)),
    _u04(
        "unclosable_state_declared",
        UnclosableStateDeclared,
        "/declaration_id",
        ("finding_unclosable",),
    ),
    _u04(
        "root_cause_analysis",
        RootCauseAnalysis,
        "/analysis_id",
        ("root_cause_analysis_signed",),
    ),
    _u04(
        "vocabulary_list_changed",
        VocabularyListChanged,
        "/change_id",
        ("vocabulary_list_changed",),
        chain_level=_ORGANIZATION,
    ),
    _u04(
        "vocabulary_list_changed",
        VocabularyListChangedV2,
        "/change_id",
        ("vocabulary_list_changed",),
        chain_level=_ORGANIZATION,
        schema_version=2,
        extra_free_text_paths=VOCABULARY_LIST_CHANGED_V2_NAME_PATHS,
    ),
    _u04(
        "action_overdue",
        ActionOverdue,
        "/overdue_id",
        ("finding_overdue", "finding_escalated"),
    ),
    _u04("unclosable_review", UnclosableReview, "/review_id", ("unclosable_reviewed",)),
    _u04("reopening", Reopening, "/reopening_id", ("finding_reopened",)),
    _u04("loop_configuration_changed", LoopConfigurationChanged, "/change_id", ()),
    _u04(
        "late_segment_attached",
        LateSegmentAttached,
        "/segment_record_id",
        ("late_segment_attached",),
    ),
    _u04(
        "closure_attachment_unrecoverable",
        ClosureAttachmentUnrecoverable,
        "/declaration_id",
        (),
    ),
)
"""Los trece tipos del lazo, en el orden de §5.1, cada uno con todas sus versiones en orden: todos
en la 1 y ``vocabulary_list_changed`` también en la 2 (TASK-350); detrás,
``closure_attachment_unrecoverable`` (VIG-274, DR-LAZ-04), que no publica eventos."""


def register_loop_record_types(registry: RecordTypeRegistry) -> None:
    """Registra los catorce tipos del lazo (con todas sus versiones) al arrancar."""
    for definition in LOOP_RECORD_TYPES:
        registry.register(definition)
