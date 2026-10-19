"""Listas cerradas de U-04 (domain-entities.md de U-04, §1, §2, §3, §5 y sus notas fechadas).

Las 28 enumeraciones de §1 más ``package_form`` (nota U04-H-03) y las seis listas que los tipos
de registro de §5.2 cierran (``subject_kind``, ``effect_on_loop``, ``escalation_level``,
``metric_unit``, ``evidence_ref_kind`` y ``blocked_term_origin``), más ``analysis_missing_item``
(TASK-315: lo que ``pending_information`` enumera, BR-LAZ-64). Dentro de una versión mayor
solo se **añaden** valores; ninguna se reduce (P4: un registro antiguo debe poder presentarse
siempre). Cada valor tiene su etiqueta en español en ``backend/resources/labels.loop.es.json``
(NFR-LAZ-52) y el arranque falla si falta una (``LOOP_LABEL_BINDINGS``,
``ACCREDITATION_LABEL_BINDINGS`` y ``NOTIFICATIONS_LABEL_BINDINGS``, una por módulo, que ata
``labels`` de cada ``PlatformUnit``).

Las heredadas (``predicate_family``, ``evidence_tier``, ``observability_state`` de U-01;
``role`` y ``coverage_state`` de U-02; ``review_reason`` y ``regression_state`` de U-03) no se
redefinen. ``export_state`` no tiene ``composing`` (U04-H-02).

``attachment_content_type`` es la excepción de forma: sus valores son tipos MIME
(``image/jpeg``), que no caben como clave de etiqueta (``snake_case``). El esquema usa
``AttachmentContentType`` y la etiqueta se busca por ``attachment_label_key`` en la lista
``AttachmentContentTypeLabel``, con los mismos tres valores escritos en ``snake_case``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final

__all__ = [
    "ACCREDITATION_LABEL_BINDINGS",
    "LOOP_LABEL_BINDINGS",
    "NOTIFICATIONS_LABEL_BINDINGS",
    "ActionType",
    "AnalysisMissingItem",
    "AssistantRequestState",
    "AssistantUnavailableCause",
    "AttachmentContentType",
    "AttachmentContentTypeLabel",
    "BlockedTermCategory",
    "BlockedTermForm",
    "BlockedTermOrigin",
    "CauseCategory",
    "ClassificationOutcome",
    "CommitmentState",
    "EffectOnLoop",
    "EscalationLevel",
    "EvidenceRefKind",
    "ExportState",
    "ExposureStatus",
    "FindingLifecycleState",
    "InboxFindingNotice",
    "InboxPartitionNotice",
    "InboxZoneNotice",
    "LoopActivityKind",
    "MetricKey",
    "MetricStatus",
    "MetricUnit",
    "MonthlyPackageState",
    "NotificationAudience",
    "NotificationChannel",
    "NotificationDeliveryStatus",
    "NotificationUrgency",
    "PackageForm",
    "ReasonCategoryAuthorized",
    "ReasonCategoryConfirmed",
    "ReasonCategoryFalsePositive",
    "ReviewResolutionOutcome",
    "RootCauseAnalysisState",
    "SampleAuditOutcome",
    "SubjectKind",
    "TextValidationVerdict",
    "UnclosableReviewOutcome",
    "attachment_label_key",
]


# --- lazo (§1) ------------------------------------------------------------------------------


class FindingLifecycleState(enum.StrEnum):
    """``finding_lifecycle_state``: el estado del hallazgo en el lazo (RF-PLA-02, respuesta 6).

    No existe un estado «sin hallazgos» ni «seguro» (P2)."""

    RECEIVED = "received"
    IN_REVIEW = "in_review"
    DISCARDED = "discarded"
    OPEN = "open"
    ACTION_ASSIGNED = "action_assigned"
    OVERDUE = "overdue"
    CLOSED = "closed"
    UNCLOSABLE = "unclosable"
    SUSPENDED_BY_RECLASSIFICATION = "suspended_by_reclassification"


class ClassificationOutcome(enum.StrEnum):
    """``classification_outcome`` (RF-PLA-01, H-21)."""

    CONFIRMED = "confirmed"
    AUTHORIZED_OPERATION = "authorized_operation"
    FALSE_POSITIVE = "false_positive"


class ReviewResolutionOutcome(enum.StrEnum):
    """``review_resolution_outcome``: los mismos valores que ``Label.outcome`` de U-02 §3.7."""

    REVIEW_CONFIRMED = "review_confirmed"
    REVIEW_DISCARDED = "review_discarded"


class ReasonCategoryConfirmed(enum.StrEnum):
    """``reason_category_confirmed`` ``[hipótesis]`` (UC1, UC2, familias de U-01)."""

    REAL_COEXISTENCE = "real_coexistence"
    GUARD_BYPASSED = "guard_bypassed"
    PROCEDURE_GAP = "procedure_gap"
    ENGINEERING_GAP = "engineering_gap"
    OTHER_CONFIRMED = "other_confirmed"


class ReasonCategoryAuthorized(enum.StrEnum):
    """``reason_category_authorized`` ``[hipótesis]`` (PRD §11 R1)."""

    SCHEDULED_MAINTENANCE = "scheduled_maintenance"
    AUTHORIZED_INTERVENTION = "authorized_intervention"
    CLEANING_OR_SETUP = "cleaning_or_setup"
    COMMISSIONING_ACTIVITY = "commissioning_activity"
    OTHER_AUTHORIZED = "other_authorized"


class ReasonCategoryFalsePositive(enum.StrEnum):
    """``reason_category_false_positive`` ``[hipótesis]`` (PRD §11, H-05)."""

    NO_PERSON_PRESENT = "no_person_present"
    REFLECTION_OR_GLARE = "reflection_or_glare"
    OBJECT_MISTAKEN_FOR_PERSON = "object_mistaken_for_person"
    INVALID_SIGNAL = "invalid_signal"
    ZONE_BOUNDARY_ERROR = "zone_boundary_error"
    OTHER_FALSE_POSITIVE = "other_false_positive"


class ActionType(enum.StrEnum):
    """``action_type``: nunca formación, amonestación ni sanción (H-24, P7)."""

    ENGINEERING = "engineering"
    PROCESS = "process"


class CauseCategory(enum.StrEnum):
    """``cause_category``: ninguna rama es una persona ni un comportamiento (H-28, Q21 = A)."""

    PROCESS = "process"
    ENGINEERING = "engineering"
    ORGANIZATION = "organization"


class RootCauseAnalysisState(enum.StrEnum):
    """``root_cause_analysis_state`` (RF-PLA-04, H-28)."""

    DRAFT = "draft"
    PENDING_INFORMATION = "pending_information"
    SIGNED = "signed"


class AnalysisMissingItem(enum.StrEnum):
    """Lo que le falta a un análisis para poder firmarse (``pending_information``, BR-LAZ-64).

    Cada valor es el nombre del campo del registro ``root_cause_analysis`` (DE §5.2) que falta."""

    WHY_STEPS = "why_steps"
    ROOT_CAUSE_CATEGORY = "root_cause_category"
    ROOT_CAUSE_STATEMENT = "root_cause_statement"
    CONCLUSION = "conclusion"


class AssistantRequestState(enum.StrEnum):
    """``assistant_request_state`` (respuesta 9)."""

    PENDING = "pending"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    REJECTED_BY_VALIDATORS = "rejected_by_validators"


class AssistantUnavailableCause(enum.StrEnum):
    """``assistant_unavailable_cause`` (RNF-RES-11, respuesta 9; nota de NFR Requirements)."""

    NO_API_KEY = "no_api_key"
    CIRCUIT_OPEN = "circuit_open"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"


class UnclosableReviewOutcome(enum.StrEnum):
    """``unclosable_review_outcome`` (respuesta 7)."""

    RENEWED = "renewed"
    CONVERTED_TO_ACTION = "converted_to_action"


class AttachmentContentType(enum.StrEnum):
    """``attachment_content_type``: el tipo MIME de un adjunto de evidencia de cierre (§6.3)."""

    IMAGE_JPEG = "image/jpeg"
    IMAGE_PNG = "image/png"
    APPLICATION_PDF = "application/pdf"


class AttachmentContentTypeLabel(enum.StrEnum):
    """Clave de etiqueta de cada ``AttachmentContentType`` (``attachment_label_key``)."""

    IMAGE_JPEG = "image_jpeg"
    IMAGE_PNG = "image_png"
    APPLICATION_PDF = "application_pdf"


def attachment_label_key(content_type: AttachmentContentType) -> AttachmentContentTypeLabel:
    """La clave de ``labels.loop.es.json`` de un tipo MIME (``image/jpeg`` → ``image_jpeg``)."""
    return AttachmentContentTypeLabel(content_type.value.replace("/", "_"))


class BlockedTermForm(enum.StrEnum):
    """``blocked_term_form`` (respuesta 10)."""

    LEMMA = "lemma"
    PATTERN = "pattern"


class BlockedTermCategory(enum.StrEnum):
    """``blocked_term_category`` (RF-PLA-15, Q23 = A, RNF-OBS-03)."""

    ABSENCE_CLAIM = "absence_claim"
    PERCENTAGE_GUARANTEE = "percentage_guarantee"
    REDUCTION_PROMISE = "reduction_promise"
    INTENT_OR_BLAME_ATTRIBUTION = "intent_or_blame_attribution"


class TextValidationVerdict(enum.StrEnum):
    """``text_validation_verdict`` (RF-PLA-15, H-30)."""

    ACCEPTED = "accepted"
    REJECTED_BLOCKED_VOCABULARY = "rejected_blocked_vocabulary"
    REJECTED_PERSON_ATTRIBUTION = "rejected_person_attribution"


class LoopActivityKind(enum.StrEnum):
    """``loop_activity_kind``: actividad por zona y día, nunca por usuario (respuesta 17)."""

    INBOX_OPENED = "inbox_opened"
    FINDING_CLASSIFIED = "finding_classified"
    QUEUE_RESOLVED = "queue_resolved"
    ANALYSIS_ADVANCED = "analysis_advanced"
    ACTION_MANAGED = "action_managed"
    FINDING_CLOSED = "finding_closed"


# --- lazo: listas que cierran los tipos de registro (§2, §5.2) ----------------------------


class SubjectKind(enum.StrEnum):
    """``subject_kind``: el sujeto del lazo (DE §2.2): un hallazgo (su episodio, respuesta 2) o una
    detección confirmada en la cola, que conserva ``detection``. Mismos valores que la
    restricción ``*_subject_kind_values`` del esquema ``loop`` (``laz_0029``)."""

    FINDING = "finding"
    DETECTION = "detection"


class InboxZoneNotice(enum.StrEnum):
    """``inbox_zone_notice``: lo que la bandeja declara de cada zona (TASK-307; BR-LAZ-04, 149 y
    153; P2). **Ninguno afirma que la zona estuviera observada** ni que no ocurriera nada: la
    bandeja no lee la cobertura del periodo (cero llamadas, NFR-LAZ-03)."""

    COVERAGE_UNAVAILABLE = "coverage_unavailable"
    """El nodo asignado comunica; la cobertura del periodo no está en la bandeja."""
    NOT_OBSERVED = "not_observed"
    """El nodo asignado está mudo o sin estado conocido: no se observó."""
    NO_NODE_ASSIGNED = "no_node_assigned"
    """La zona no tiene nodo asignado: no se observó."""


class InboxPartitionNotice(enum.StrEnum):
    """``inbox_partition_notice``: la proyección de una planta que no se puede leer como el
    expediente (TASK-307; BR-LAZ-156); la bandeja lo declara en vez de inventar un estado."""

    PROJECTION_REBUILDING = "projection_rebuilding"
    PROJECTION_DIVERGED = "projection_diverged"


class InboxFindingNotice(enum.StrEnum):
    """``inbox_finding_notice``: los avisos de un elemento, desde las columnas enriquecidas de
    ``FindingState`` (TASK-307; BR-LAZ-10, 11, 150 y 155)."""

    AGREEMENT_REVOKED = "agreement_revoked"
    WALK_TEST_REGRESSION_PENDING = "walk_test_regression_pending"
    EVIDENCE_FLAGGED = "evidence_flagged"
    MISSING_SEGMENTS_DECLARED = "missing_segments_declared"


class EffectOnLoop(enum.StrEnum):
    """``effect_on_loop`` de una clasificación (§2.3)."""

    NONE = "none"
    LOOP_OPENED = "loop_opened"
    LOOP_SUSPENDED = "loop_suspended"
    LOOP_RESUMED = "loop_resumed"
    CLOSED_MARKED = "closed_marked"


class EscalationLevel(enum.StrEnum):
    """``escalation_level`` de ``action_overdue`` (§5.2, nota U04-H-07)."""

    COORDINATOR = "coordinator"
    MANAGER_WEEKLY = "manager_weekly"


class EvidenceRefKind(enum.StrEnum):
    """``kind`` de una referencia de evidencia de un paso del análisis (§2.9)."""

    RECORD = "record"
    CLIP = "clip"
    METRIC = "metric"
    COVERAGE = "coverage"


class BlockedTermOrigin(enum.StrEnum):
    """``origin`` de un término bloqueado (§2.12): el mínimo irreductible o el del cliente."""

    DERIVED = "derived"
    CUSTOMER_ADDED = "customer_added"


# --- acreditación (§1, §3) ------------------------------------------------------------------


class ExportState(enum.StrEnum):
    """``export_state`` (respuesta 11): manda sobre el de U-05; sin ``composing`` (U04-H-02)."""

    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


class PackageForm(enum.StrEnum):
    """``package_form`` de la exportación (nota U04-H-03)."""

    WITH_CLIPS = "with_clips"
    WITHOUT_CLIPS = "without_clips"


class MonthlyPackageState(enum.StrEnum):
    """``monthly_package_state`` (respuesta 13)."""

    GENERATING = "generating"
    SEALED = "sealed"
    FAILED = "failed"


class MetricKey(enum.StrEnum):
    """``metric_key`` (RF-PLA-17, respuesta 14): ninguna mide productividad (P7)."""

    PRIMARY_ENGINEERING_CLOSURES = "primary_engineering_closures"
    ACTIVATION = "activation"
    ADOPTION = "adoption"
    CLASSIFIED_IN_TIME = "classified_in_time"
    OVERDUE = "overdue"
    RECURRENCE = "recurrence"
    EXPOSURE = "exposure"


class MetricStatus(enum.StrEnum):
    """``metric_status``: sin insumo completo, ``not_computable``, nunca cero (RF-PLA-19)."""

    COMPUTED = "computed"
    PARTIAL = "partial"
    NOT_COMPUTABLE = "not_computable"


class MetricUnit(enum.StrEnum):
    """``unit`` de un ``MetricValue`` (§3.4)."""

    COUNT = "count"
    RATIO = "ratio"
    MINUTES = "minutes"


class ExposureStatus(enum.StrEnum):
    """``exposure_status`` (RF-PLA-19, respuesta 15)."""

    COMPUTED = "computed"
    PARTIAL = "partial"
    NOT_COMPUTABLE = "not_computable"


class SampleAuditOutcome(enum.StrEnum):
    """``sample_audit_outcome`` (respuesta 13, H-39): la auditoría no cambia clasificaciones."""

    CONSISTENT_CRITERION = "consistent_criterion"
    DOUBTFUL_CRITERION = "doubtful_criterion"
    CRITERION_ERROR = "criterion_error"


class CommitmentState(enum.StrEnum):
    """``commitment_state`` (RF-PLA-13, H-31)."""

    OPEN = "open"
    DONE = "done"
    DROPPED = "dropped"


# --- notificaciones (§1, §6) ----------------------------------------------------------------


class NotificationUrgency(enum.StrEnum):
    """``notification_urgency`` (respuesta 16): ``immediate`` no se puede silenciar."""

    IMMEDIATE = "immediate"
    NORMAL = "normal"
    DIGEST = "digest"


class NotificationChannel(enum.StrEnum):
    """``notification_channel`` (RF-PLA-16, Q17 = B)."""

    IN_APP = "in_app"
    EMAIL = "email"


class NotificationDeliveryStatus(enum.StrEnum):
    """``notification_delivery_status`` (respuesta 16, RNF-RES-11)."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED_BY_PREFERENCE = "suppressed_by_preference"
    DEGRADED_APP_ONLY = "degraded_app_only"


class NotificationAudience(enum.StrEnum):
    """``notification_audience`` (respuesta 16, S-PLA-16)."""

    CLIENT = "client"
    PROVIDER = "provider"
    BOTH = "both"


# --- etiquetas ------------------------------------------------------------------------------


LOOP_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "finding_lifecycle_state": FindingLifecycleState,
    "classification_outcome": ClassificationOutcome,
    "review_resolution_outcome": ReviewResolutionOutcome,
    "reason_category_confirmed": ReasonCategoryConfirmed,
    "reason_category_authorized": ReasonCategoryAuthorized,
    "reason_category_false_positive": ReasonCategoryFalsePositive,
    "action_type": ActionType,
    "cause_category": CauseCategory,
    "root_cause_analysis_state": RootCauseAnalysisState,
    "analysis_missing_item": AnalysisMissingItem,
    "assistant_request_state": AssistantRequestState,
    "assistant_unavailable_cause": AssistantUnavailableCause,
    "unclosable_review_outcome": UnclosableReviewOutcome,
    "attachment_content_type": AttachmentContentTypeLabel,
    "blocked_term_form": BlockedTermForm,
    "blocked_term_category": BlockedTermCategory,
    "text_validation_verdict": TextValidationVerdict,
    "loop_activity_kind": LoopActivityKind,
    "subject_kind": SubjectKind,
    "effect_on_loop": EffectOnLoop,
    "escalation_level": EscalationLevel,
    "evidence_ref_kind": EvidenceRefKind,
    "blocked_term_origin": BlockedTermOrigin,
    "inbox_zone_notice": InboxZoneNotice,
    "inbox_partition_notice": InboxPartitionNotice,
    "inbox_finding_notice": InboxFindingNotice,
}
"""Enumeraciones del lazo con etiqueta en ``labels.loop.es.json`` (NFR-LAZ-52)."""

ACCREDITATION_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "export_state": ExportState,
    "package_form": PackageForm,
    "monthly_package_state": MonthlyPackageState,
    "metric_key": MetricKey,
    "metric_status": MetricStatus,
    "metric_unit": MetricUnit,
    "exposure_status": ExposureStatus,
    "sample_audit_outcome": SampleAuditOutcome,
    "commitment_state": CommitmentState,
}
"""Enumeraciones de la acreditación con etiqueta en ``labels.loop.es.json``."""

NOTIFICATIONS_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "notification_urgency": NotificationUrgency,
    "notification_channel": NotificationChannel,
    "notification_delivery_status": NotificationDeliveryStatus,
    "notification_audience": NotificationAudience,
}
"""Enumeraciones de las notificaciones con etiqueta en ``labels.loop.es.json``."""
