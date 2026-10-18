"""Eventos del lazo en la bandeja (domain-entities.md de U-04, §11.3 y nota R14 de §7).

Veintiuno de los 25 eventos que publica U-04; los otros cuatro (exportación, paquete mensual,
compromisos y adopción baja) están en ``accreditation.events``, y ``notifications`` no publica
ninguno (consume). ``user_invited`` no es de U-04: lo envía U-02 (U04-H-13).

La carga de cada uno lleva **solo identificadores, valores de listas cerradas, enteros y marcas**,
hasta 64 KB (BR-NUC-75, ``shared.outbox.publish.MAX_PAYLOAD_BYTES``): nunca texto libre, el
contenido del registro ni datos de personas. Quien necesite el detalle lo lee del expediente con
``record_id`` (``LectorExpediente``). La organización, la planta, ``event_id``, ``occurred_at`` y
``correlation_id`` van en el propio evento, no en la carga. Una carga solo puede ampliarse con
campos opcionales (``OutboxCatalog.synchronize``).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StrictBool, StrictInt
from vigia_contracts.models.common import UUID, Timestamp, UUIDv7
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.loop.domain.enums import (
    ActionType,
    CauseCategory,
    ClassificationOutcome,
    EscalationLevel,
    FindingLifecycleState,
    SubjectKind,
    UnclosableReviewOutcome,
)
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry, PayloadModel

__all__ = ["LOOP_EVENT_TYPES", "register_loop_event_types"]

Count = Annotated[StrictInt, Field(ge=0, le=10_000_000)]
Days = Annotated[StrictInt, Field(ge=0, le=3_660)]


class _Subject(PayloadModel):
    zone_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID


class FindingClassified(_Subject):
    """``finding_confirmed`` y ``finding_discarded``: una clasificación con su salida."""

    classification_id: UUIDv7
    record_id: UUID
    outcome: ClassificationOutcome
    family: PredicateFamily


class FindingReclassified(FindingClassified):
    previous_outcome: ClassificationOutcome


class LoopSuspensionChanged(_Subject):
    """``loop_suspended`` y ``loop_resumed``: el efecto de una reclasificación sobre el lazo."""

    classification_id: UUIDv7
    record_id: UUID


class ReviewResolved(PayloadModel):
    """``review_confirmed`` y ``review_discarded``."""

    zone_id: UUID
    detection_id: UUID
    resolution_id: UUIDv7
    record_id: UUID
    family: PredicateFamily


class ReviewOverdue(PayloadModel):
    """Un elemento de la cola sin revisar pasado su plazo; ``reminders_sent`` cuenta los avisos."""

    zone_id: UUID
    detection_id: UUID
    review_due_at: Timestamp
    reminders_sent: Count


class ActionAssigned(_Subject):
    action_id: UUIDv7
    record_id: UUID
    action_type: ActionType
    assignee_user_id: UUID
    due_at: Timestamp


class ActionEvidenceAdded(PayloadModel):
    zone_id: UUID
    subject_id: UUID
    action_id: UUIDv7
    evidence_entry_id: UUIDv7
    record_id: UUID
    attachment_count: Annotated[StrictInt, Field(ge=0, le=5)]


class FindingClosed(_Subject):
    closure_id: UUIDv7
    record_id: UUID
    action_id: UUIDv7
    action_type: ActionType


class FindingUnclosable(_Subject):
    declaration_id: UUIDv7
    record_id: UUID
    review_due_at: Timestamp


class UnclosableReviewDue(PayloadModel):
    """Llegó ``review_due_at`` de una declaración: no cambia el estado del hallazgo (§2.7)."""

    zone_id: UUID
    subject_id: UUID
    declaration_id: UUIDv7
    review_due_at: Timestamp


class UnclosableReviewed(PayloadModel):
    zone_id: UUID
    subject_id: UUID
    declaration_id: UUIDv7
    review_id: UUIDv7
    record_id: UUID
    outcome: UnclosableReviewOutcome


class FindingReopened(_Subject):
    reopening_id: UUIDv7
    record_id: UUID
    from_state: Literal[FindingLifecycleState.CLOSED, FindingLifecycleState.UNCLOSABLE]
    new_action_id: UUIDv7


class FindingOverdue(_Subject):
    """``finding_overdue`` y ``finding_escalated``: un registro ``action_overdue`` por nivel."""

    action_id: UUIDv7
    overdue_id: UUID
    record_id: UUID
    due_at: Timestamp
    days_overdue: Days
    escalation_level: EscalationLevel
    assignee_user_id: UUID


class RootCauseAnalysisSigned(_Subject):
    analysis_id: UUIDv7
    record_id: UUID
    root_cause_category: CauseCategory


class VocabularyListChanged(PayloadModel):
    """Versión nueva de la lista de la organización: refresca la caché por proceso (§2.12).

    Nunca lleva los términos: solo la versión y los recuentos."""

    change_id: UUIDv7
    record_id: UUID
    list_version: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    added_count: Annotated[StrictInt, Field(ge=0, le=200)]
    removed_count: Annotated[StrictInt, Field(ge=0, le=200)]


class LateSegmentAttached(PayloadModel):
    zone_id: UUID
    episode_id: UUID
    segment_record_id: UUID
    record_id: UUID
    segment_index: Annotated[StrictInt, Field(ge=0, le=2**31 - 1)]


class LoopProjectionDivergence(PayloadModel):
    """La proyección de la planta difiere del expediente (alarma; el expediente manda, §9)."""

    rows_checked: Count
    rows_diverged: Annotated[StrictInt, Field(ge=1, le=10_000_000)]
    full_scan: StrictBool
    divergence_count: Annotated[StrictInt, Field(ge=1, le=1_000_000)]


LOOP_EVENT_TYPES: Final[tuple[EventType, ...]] = tuple(
    EventType(
        event_name=name,
        publisher_unit=ActorUnit.U04,
        payload_model=model,
        description_es=description,
    )
    for name, model, description in (
        ("finding_confirmed", FindingClassified, "Un hallazgo se clasificó como confirmado"),
        (
            "finding_discarded",
            FindingClassified,
            "Un hallazgo se clasificó como operación autorizada o como falso positivo",
        ),
        (
            "finding_reclassified",
            FindingReclassified,
            "Un hallazgo se reclasificó con motivo reforzado; la clasificación anterior queda",
        ),
        (
            "loop_suspended",
            LoopSuspensionChanged,
            "Una reclasificación suspendió el lazo del hallazgo",
        ),
        ("loop_resumed", LoopSuspensionChanged, "Una reclasificación reanudó el lazo"),
        (
            "review_confirmed",
            ReviewResolved,
            "Una detección de la cola de revisión se confirmó en revisión",
        ),
        (
            "review_discarded",
            ReviewResolved,
            "Una detección de la cola de revisión se descartó en revisión",
        ),
        (
            "review_overdue",
            ReviewOverdue,
            "Un elemento de la cola de revisión sigue sin revisar pasado su plazo",
        ),
        (
            "action_assigned",
            ActionAssigned,
            "Se asignó una acción de ingeniería o de proceso con responsable y plazo",
        ),
        (
            "action_evidence_added",
            ActionEvidenceAdded,
            "Se añadió evidencia de la ejecución de una acción",
        ),
        ("finding_closed", FindingClosed, "Un hallazgo se cerró con acción, evidencia y firma"),
        (
            "finding_unclosable",
            FindingUnclosable,
            "Se declaró un hallazgo SIN ALTERNATIVA SEGURA DISPONIBLE",
        ),
        (
            "unclosable_review_due",
            UnclosableReviewDue,
            "Venció la fecha de revisión de una declaración de incerrable",
        ),
        (
            "unclosable_reviewed",
            UnclosableReviewed,
            "Se revisó una declaración de incerrable: renovada o convertida en acción",
        ),
        ("finding_reopened", FindingReopened, "Un hallazgo cerrado o incerrable se reabrió"),
        ("finding_overdue", FindingOverdue, "Venció el plazo de la acción de un hallazgo"),
        (
            "finding_escalated",
            FindingOverdue,
            "Un plazo vencido se escaló al siguiente nivel de aviso",
        ),
        (
            "root_cause_analysis_signed",
            RootCauseAnalysisSigned,
            "Se firmó el análisis de causa raíz de un hallazgo",
        ),
        (
            "vocabulary_list_changed",
            VocabularyListChanged,
            "La lista de vocabulario bloqueado de la organización tiene una versión nueva",
        ),
        (
            "late_segment_attached",
            LateSegmentAttached,
            "Un tramo tardío se adjuntó a su episodio sin reabrir el lazo",
        ),
        (
            "loop_projection_divergence",
            LoopProjectionDivergence,
            "La proyección del lazo de una planta difiere del expediente",
        ),
    )
)
"""Los veintiún eventos del lazo, en el orden de §11.3."""


def register_loop_event_types(registry: EventTypeRegistry) -> None:
    """Registra los veintiún eventos del lazo al arrancar."""
    for event_type in LOOP_EVENT_TYPES:
        registry.register(event_type)
