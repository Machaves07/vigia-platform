"""Tablas del esquema ``loop`` en SQLAlchemy Core (TASK-301, LC-LAZ-02).

Reflejan la migración ``laz_0029_loop_schema``, que es la fuente de verdad: las restricciones
(listas cerradas, longitudes, claves foráneas hacia ``identity``), la seguridad a nivel de fila,
las particiones mensuales, los índices parciales, los disparadores de solo anexar y los permisos
viven allí y no se repiten aquí. Estas definiciones solo dan nombre y tipo a las columnas para
los repositorios de U-04; ``tests/integration/test_laz_loop_schema.py`` comprueba que coinciden
con la base.

Sin ORM y sin repositorios: toda consulta se abre con un ``ScopeContext`` (BR-NUC-02) desde la
tarea que la necesite. Las marcas de tiempo no tienen valor por defecto: las pone la aplicación
con su ``Clock``.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import BigInteger, Boolean, Column, Date, Double, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import TIMESTAMP

__all__ = [
    "ACTION_EVIDENCE_ENTRY",
    "ASSISTANT_DRAFT",
    "ASSISTANT_REQUEST",
    "BLOCKED_TERM",
    "BLOCKED_VOCABULARY_LIST",
    "CLASSIFICATION",
    "CLOSURE_ATTACHMENT",
    "CLOSURE_RECORD",
    "CORRECTIVE_ACTION",
    "EPISODE",
    "EPISODE_REBUILD_SHADOW",
    "FINDING_STATE",
    "FINDING_STATE_REBUILD_SHADOW",
    "LOOP_PROJECTION_CHECKPOINT",
    "LOOP_PROJECTION_SAMPLE_RUN",
    "METADATA",
    "PLANT_LOOP_CONFIGURATION",
    "REOPENING",
    "REVIEW_QUEUE_ITEM",
    "REVIEW_RESOLUTION",
    "ROOT_CAUSE_ANALYSIS",
    "SCHEMA",
    "UNCLOSABLE_DECLARATION",
    "UNCLOSABLE_REVIEW",
]

SCHEMA: Final = "loop"
METADATA: Final = MetaData(schema=SCHEMA)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), nullable=nullable, primary_key=primary_key)


def _instant(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable, primary_key=primary_key)


def _text(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable)


def _jsonb(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, JSONB, nullable=nullable)


def _integer(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Integer, nullable=nullable)


def _boolean(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Boolean, nullable=nullable)


def _zone_scope() -> tuple[Column[Any], Column[Any], Column[Any]]:
    """``organization_id``, ``plant_id`` y ``zone_id``: las tablas de zona los llevan."""
    return _uuid("organization_id"), _uuid("plant_id"), _uuid("zone_id")


def _subject() -> tuple[Column[Any], Column[Any]]:
    """El sujeto del lazo: episodio (``finding``) o detección confirmada (``detection``)."""
    return _text("subject_kind"), _uuid("subject_id")


EPISODE: Final = Table(
    "episode",
    METADATA,
    _uuid("episode_id", primary_key=True),
    *_zone_scope(),
    _text("family"),
    _text("tier"),
    _uuid("standard_id"),
    _integer("standard_version"),
    Column("segment_record_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _integer("segment_count"),
    _integer("expected_segment_count"),
    Column("missing_segment_indexes", ARRAY(Integer), nullable=False),
    _instant("started_at"),
    _instant("ended_at"),
    Column("total_episode_duration_ms", BigInteger, nullable=False),
    Column("total_condition_duration_ms", BigInteger, nullable=False),
    Column("max_confidence", Double, nullable=False),
    _jsonb("clip_references"),
    _text("observability_state_at_start"),
    _boolean("continues_open"),
    _instant("last_segment_received_at"),
    _uuid("last_event_id"),
    _instant("projection_updated_at"),
)
"""§2.1 ``Episode`` 🔁."""

FINDING_STATE: Final = Table(
    "finding_state",
    METADATA,
    _uuid("finding_state_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("anchor_record_id"),
    _text("family"),
    _text("tier"),
    _uuid("standard_id"),
    _integer("standard_version"),
    _text("lifecycle_state"),
    _text("previous_lifecycle_state", nullable=True),
    _text("current_classification_outcome", nullable=True),
    _text("current_reason_category", nullable=True),
    _integer("classification_count"),
    _instant("first_classified_at", nullable=True),
    _instant("last_classified_at", nullable=True),
    _boolean("classified_within_deadline", nullable=True),
    _uuid("current_action_id", nullable=True),
    _text("current_action_type", nullable=True),
    _uuid("assignee_user_id", nullable=True),
    _boolean("assignee_inactive"),
    _instant("due_at", nullable=True),
    _instant("overdue_since", nullable=True),
    _integer("overdue_days"),
    _instant("closed_at", nullable=True),
    _uuid("closure_record_id", nullable=True),
    _instant("unclosable_since", nullable=True),
    _instant("unclosable_review_due_at", nullable=True),
    _boolean("unclosable_pending_review"),
    _boolean("closed_with_later_different_classification"),
    _integer("reopening_count"),
    _boolean("has_signed_root_cause"),
    _text("observability_state_at_event"),
    _text("zone_gate_status_at_event"),
    _boolean("zone_agreement_revoked"),
    _instant("zone_agreement_revoked_at", nullable=True),
    _boolean("single_occupancy_zone"),
    _integer("aggregation_window_minutes"),
    _boolean("walk_test_regression_pending"),
    _text("walk_test_regression_cause", nullable=True),
    _instant("occurred_at_exact"),
    _instant("occurred_at_bucket", primary_key=True),
    _integer("age_days"),
    _boolean("evidence_flagged"),
    _boolean("missing_segments_declared"),
    _uuid("last_event_id"),
    _uuid("last_record_id"),
    _instant("projection_updated_at"),
    _text("projection_digest"),
)
"""§2.2 ``FindingState`` 🔁, particionada por mes de ``occurred_at_bucket`` (en la clave)."""

CLASSIFICATION: Final = Table(
    "classification",
    METADATA,
    _uuid("classification_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("record_id"),
    _integer("sequence_in_subject"),
    _text("outcome"),
    _text("reason_category"),
    _text("reason_text"),
    _boolean("is_reclassification"),
    _boolean("reinforced_reason"),
    _text("previous_outcome", nullable=True),
    _text("effect_on_loop"),
    _jsonb("classified_by"),
    _instant("classified_at", primary_key=True),
    _uuid("label_id"),
    _uuid("correlation_id"),
)
"""§2.3 ``Classification`` 🔁, particionada por mes de ``classified_at``; ninguna fila cambia."""

REVIEW_QUEUE_ITEM: Final = Table(
    "review_queue_item",
    METADATA,
    _uuid("detection_id", primary_key=True),
    *_zone_scope(),
    _uuid("node_id"),
    _uuid("anchor_record_id"),
    _text("family"),
    _uuid("standard_id"),
    _integer("standard_version"),
    Column("max_confidence", Double, nullable=False),
    Column("review_threshold", Double, nullable=False),
    Column("publication_threshold", Double, nullable=False),
    _text("review_reason"),
    _text("observability_state_at_event"),
    _instant("occurred_at_exact"),
    _instant("occurred_at_bucket"),
    _jsonb("clip_references"),
    _jsonb("signals_summary"),
    _instant("received_at"),
    _instant("review_due_at"),
    _text("state"),
    _instant("resolved_at", nullable=True),
    _instant("reminder_sent_at", nullable=True),
    _integer("reminders_sent"),
    _uuid("source_event_id"),
)
"""§2.4 ``ReviewQueueItem`` 🔁."""

REVIEW_RESOLUTION: Final = Table(
    "review_resolution",
    METADATA,
    _uuid("resolution_id", primary_key=True),
    *_zone_scope(),
    _uuid("detection_id"),
    _uuid("record_id"),
    _text("outcome"),
    _text("reason_category"),
    _text("reason_text"),
    _jsonb("resolved_by"),
    _instant("resolved_at", primary_key=True),
    _uuid("label_id"),
    _uuid("correlation_id"),
)
"""§2.4 ``ReviewResolution`` 🔁, particionada por mes de ``resolved_at``."""

CORRECTIVE_ACTION: Final = Table(
    "corrective_action",
    METADATA,
    _uuid("action_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("record_id"),
    _text("action_type"),
    _text("action_description"),
    _text("expected_outcome", nullable=True),
    _uuid("assignee_user_id"),
    _jsonb("assigned_by"),
    _instant("assigned_at"),
    _instant("due_at"),
    _text("state"),
    _uuid("root_cause_analysis_id", nullable=True),
    _boolean("counts_as_overdue"),
)
"""§2.5 ``CorrectiveAction`` 🔁; ``due_at`` congelado al asignarla."""

ACTION_EVIDENCE_ENTRY: Final = Table(
    "action_evidence_entry",
    METADATA,
    _uuid("evidence_entry_id", primary_key=True),
    *_zone_scope(),
    _uuid("action_id"),
    _uuid("record_id"),
    _text("description"),
    _text("verification_note", nullable=True),
    _instant("performed_at"),
    _text("performed_by_role"),
    Column("attachment_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _jsonb("added_by"),
    _instant("added_at"),
)
"""§2.6 ``ActionEvidenceEntry`` 🔁, con la planta y la zona de su acción."""

CLOSURE_ATTACHMENT: Final = Table(
    "closure_attachment",
    METADATA,
    _uuid("attachment_id", primary_key=True),
    *_zone_scope(),
    _uuid("subject_id"),
    _text("content_type"),
    Column("size_bytes", BigInteger, nullable=False),
    _text("sha256"),
    _text("filename"),
    _text("storage_key"),
    _boolean("no_identifiable_person_declared"),
    _jsonb("declared_by"),
    _instant("uploaded_at"),
    _instant("verified_at", nullable=True),
)
"""§2.6 y §6.3 ``ClosureEvidenceAttachment`` ⛓; cierre ``verified_at``."""

CLOSURE_RECORD: Final = Table(
    "closure_record",
    METADATA,
    _uuid("closure_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("action_id"),
    _uuid("record_id"),
    _text("closure_description"),
    _text("verification_note", nullable=True),
    Column("evidence_entry_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    Column("attachment_digests", ARRAY(Text), nullable=False),
    _jsonb("signer"),
    _instant("signed_at"),
    _uuid("correlation_id"),
)
"""§2.6 ``ClosureRecord`` 🔁."""

UNCLOSABLE_DECLARATION: Final = Table(
    "unclosable_declaration",
    METADATA,
    _uuid("declaration_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("record_id"),
    _text("evaluated_alternative"),
    _text("rejection_reason"),
    _uuid("responsible_user_id"),
    Column("decision_date", Date, nullable=False),
    _text("compensatory_measure"),
    _instant("review_due_at"),
    _uuid("policy_id"),
    _text("policy_version"),
    _text("policy_document_sha256"),
    _jsonb("signer"),
    _instant("declared_at"),
    _text("state"),
)
"""§2.7 ``UnclosableDeclaration`` 🔁: los cinco campos a la vez."""

UNCLOSABLE_REVIEW: Final = Table(
    "unclosable_review",
    METADATA,
    _uuid("review_id", primary_key=True),
    *_zone_scope(),
    _uuid("declaration_id"),
    _uuid("record_id"),
    _text("outcome"),
    _text("review_note"),
    _instant("new_review_due_at", nullable=True),
    _uuid("new_action_id", nullable=True),
    _jsonb("reviewed_by"),
    _instant("reviewed_at"),
)
"""§2.7 ``UnclosableReview`` 🔁, con la planta y la zona de su declaración."""

REOPENING: Final = Table(
    "reopening",
    METADATA,
    _uuid("reopening_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("record_id"),
    _text("from_state"),
    _uuid("previous_closure_id", nullable=True),
    _uuid("previous_declaration_id", nullable=True),
    _text("reason_text"),
    _uuid("new_action_id"),
    _jsonb("reopened_by"),
    _instant("reopened_at"),
)
"""§2.8 ``Reopening`` 🔁."""

ROOT_CAUSE_ANALYSIS: Final = Table(
    "root_cause_analysis",
    METADATA,
    _uuid("analysis_id", primary_key=True),
    *_zone_scope(),
    *_subject(),
    _uuid("record_id", nullable=True),
    _text("state"),
    _jsonb("zone_context"),
    _jsonb("why_steps"),
    _jsonb("cause_effect_branches"),
    _text("root_cause_category", nullable=True),
    _text("root_cause_statement", nullable=True),
    _text("conclusion", nullable=True),
    _text("missing_information", nullable=True),
    _boolean("assistant_used"),
    _text("assistant_model", nullable=True),
    _text("assistant_version", nullable=True),
    _boolean("assistant_text_edited", nullable=True),
    _jsonb("signer", nullable=True),
    _instant("signed_at", nullable=True),
    _instant("created_at"),
    _instant("updated_at"),
)
"""§2.9 ``RootCauseAnalysis`` 🔁: borrador, pendiente de información o firmado."""

ASSISTANT_REQUEST: Final = Table(
    "assistant_request",
    METADATA,
    _uuid("request_id", primary_key=True),
    *_zone_scope(),
    _uuid("analysis_id"),
    _uuid("user_id"),
    _jsonb("requested_by"),
    _instant("requested_at"),
    _text("state"),
    _text("unavailable_cause", nullable=True),
    _instant("resolved_at", nullable=True),
    _uuid("outbox_event_id"),
    _integer("attempt_count"),
    _text("context_digest"),
    Column("input_tokens", BigInteger, nullable=False),
    Column("output_tokens", BigInteger, nullable=False),
    Column("cache_read_tokens", BigInteger, nullable=False),
    Column("provider_duration_ms", BigInteger, nullable=False),
    _text("response_model", nullable=True),
    _text("inference_geo", nullable=True),
    _text("stop_reason", nullable=True),
    _text("provider_request_id", nullable=True),
    _text("rejection_reason", nullable=True),
)
"""§2.11 ``AssistantRequest`` ⛓ (solo organización, A-69); el límite exacto se cuenta sobre
``(organization_id, user_id, requested_at)``. El uso por solicitud (fichas, duración, modelo de
la respuesta, región y motivo de parada) es de ``laz_0033`` (TASK-316, NFR-LAZ-31); el motivo
cerrado de ``rejected_by_validators``, de ``laz_0041`` (VIG-344)."""

ASSISTANT_DRAFT: Final = Table(
    "assistant_draft",
    METADATA,
    _uuid("draft_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("request_id"),
    _text("draft_text"),
    _jsonb("citations"),
    _boolean("insufficient_evidence_declared"),
    _text("model_name"),
    _text("model_version"),
    _text("provider"),
    _jsonb("validation_results"),
    _boolean("accepted_by_human"),
    _boolean("edited_by_human"),
    _instant("created_at"),
    _text("proposed_cause_category", nullable=True),
)
"""§2.11 ``AssistantDraft`` (solo organización, A-69); ``proposed_cause_category`` es de
``laz_0033`` (TASK-316)."""

BLOCKED_VOCABULARY_LIST: Final = Table(
    "blocked_vocabulary_list",
    METADATA,
    _uuid("list_id", primary_key=True),
    _uuid("organization_id"),
    _integer("version"),
    _instant("effective_from"),
    _jsonb("created_by"),
    _uuid("record_id"),
    _text("lemmatizer_version", nullable=True),
    _jsonb("declared_names"),
)
"""§2.12 ``BlockedVocabularyList`` 🔁 (solo organización, A-69): una por organización;
``declared_names`` es de ``laz_0039`` (TASK-350)."""

BLOCKED_TERM: Final = Table(
    "blocked_term",
    METADATA,
    _uuid("term_id", primary_key=True),
    _uuid("list_id"),
    _uuid("organization_id"),
    _text("category"),
    _text("form"),
    _text("value"),
    _text("suggestion_es"),
    _text("origin"),
    _integer("added_in_version"),
    _integer("removed_in_version", nullable=True),
    _text("added_reason"),
)
"""§2.12 ``BlockedTerm`` 🔁 (solo organización, A-69)."""

PLANT_LOOP_CONFIGURATION: Final = Table(
    "plant_loop_configuration",
    METADATA,
    _uuid("plant_id", primary_key=True),
    _uuid("organization_id"),
    _integer("classification_deadline_business_days"),
    _integer("review_deadline_business_days"),
    _integer("action_deadline_default_days"),
    _integer("action_deadline_min_days"),
    _integer("action_deadline_max_days"),
    _integer("manager_highlight_overdue_days"),
    _integer("unclosable_review_default_months"),
    _integer("unclosable_review_max_months"),
    _integer("monthly_package_generation_day"),
    _integer("sample_percent"),
    _integer("sample_min"),
    _integer("sample_max"),
    _integer("adoption_silence_days"),
    _integer("exposure_coverage_threshold_percent"),
    _integer("email_grouping_hours"),
    _integer("export_max_days"),
    _integer("export_max_zones"),
    _integer("single_occupancy_default_window_minutes"),
    _integer("reminder_interval_hours"),
    _jsonb("business_calendar"),
    _text("timezone"),
    _integer("version"),
    _instant("changed_at"),
    _jsonb("changed_by"),
    _text("change_reason"),
)
"""§2.13 ``PlantLoopConfiguration`` 🔁, una fila por planta (con ``reminder_interval_hours``,
U04-H-07)."""

LOOP_PROJECTION_CHECKPOINT: Final = Table(
    "loop_projection_checkpoint",
    METADATA,
    _uuid("organization_id", primary_key=True),
    _uuid("plant_id", primary_key=True),
    Column("last_applied_ledger_sequence", BigInteger, nullable=False),
    Column("ingestion_frontier", BigInteger, nullable=False),
    _uuid("last_applied_event_id", nullable=True),
    _text("projection_digest", nullable=True),
    _instant("rebuilt_at", nullable=True),
    _instant("last_verified_at", nullable=True),
    _integer("divergence_count"),
    _instant("last_divergence_at", nullable=True),
    _text("state"),
    _uuid("rebuild_owner", nullable=True),
    _instant("rebuild_lease_until", nullable=True),
)
"""§2.14 ``LoopProjectionCheckpoint`` (solo organización, A-69): uno por (organización, planta).
``rebuild_owner``/``rebuild_lease_until`` son el arrendamiento de la reconstrucción (TASK-306,
``laz_0037``, PAT-NUC-RES-05)."""

EPISODE_REBUILD_SHADOW: Final = Table(
    "episode_rebuild_shadow",
    METADATA,
    _uuid("episode_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id", primary_key=True),
    _uuid("zone_id"),
    _text("family"),
    _text("tier"),
    _uuid("standard_id"),
    _integer("standard_version"),
    Column("segment_record_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _integer("segment_count"),
    _integer("expected_segment_count"),
    Column("missing_segment_indexes", ARRAY(Integer), nullable=False),
    _instant("started_at"),
    _instant("ended_at"),
    Column("total_episode_duration_ms", BigInteger, nullable=False),
    Column("total_condition_duration_ms", BigInteger, nullable=False),
    Column("max_confidence", Double, nullable=False),
    _jsonb("clip_references"),
    _text("observability_state_at_start"),
    _boolean("continues_open"),
    _instant("last_segment_received_at"),
    _uuid("last_event_id"),
    _instant("projection_updated_at"),
)
"""Tabla sombra de la reconstrucción por planta para ``episode`` (TASK-306, LC-LAZ-03,
``laz_0037``): mismas columnas, sin ``observability_state_at_start`` fijado por FK alguna."""

FINDING_STATE_REBUILD_SHADOW: Final = Table(
    "finding_state_rebuild_shadow",
    METADATA,
    _uuid("finding_state_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id", primary_key=True),
    _uuid("zone_id"),
    _text("subject_kind"),
    _uuid("subject_id"),
    _uuid("anchor_record_id"),
    _text("family"),
    _text("tier"),
    _uuid("standard_id"),
    _integer("standard_version"),
    _text("lifecycle_state"),
    _text("previous_lifecycle_state", nullable=True),
    _text("current_classification_outcome", nullable=True),
    _text("current_reason_category", nullable=True),
    _integer("classification_count"),
    _instant("first_classified_at", nullable=True),
    _instant("last_classified_at", nullable=True),
    _boolean("classified_within_deadline", nullable=True),
    _uuid("current_action_id", nullable=True),
    _text("current_action_type", nullable=True),
    _uuid("assignee_user_id", nullable=True),
    _boolean("assignee_inactive"),
    _instant("due_at", nullable=True),
    _instant("overdue_since", nullable=True),
    _integer("overdue_days"),
    _instant("closed_at", nullable=True),
    _uuid("closure_record_id", nullable=True),
    _instant("unclosable_since", nullable=True),
    _instant("unclosable_review_due_at", nullable=True),
    _boolean("unclosable_pending_review"),
    _boolean("closed_with_later_different_classification"),
    _integer("reopening_count"),
    _boolean("has_signed_root_cause"),
    _text("observability_state_at_event"),
    _text("zone_gate_status_at_event"),
    _boolean("zone_agreement_revoked"),
    _instant("zone_agreement_revoked_at", nullable=True),
    _boolean("single_occupancy_zone"),
    _integer("aggregation_window_minutes"),
    _boolean("walk_test_regression_pending"),
    _text("walk_test_regression_cause", nullable=True),
    _instant("occurred_at_exact"),
    _instant("occurred_at_bucket"),
    _integer("age_days"),
    _boolean("evidence_flagged"),
    _boolean("missing_segments_declared"),
    _uuid("last_event_id"),
    _uuid("last_record_id"),
    _instant("projection_updated_at"),
    _text("projection_digest"),
)
"""Tabla sombra de la reconstrucción por planta para ``finding_state`` (TASK-306, LC-LAZ-03,
``laz_0037``): sin la partición mensual de la tabla en servicio (no es ``finding_state``,
``occurred_at_bucket`` no es parte de la clave aquí)."""

LOOP_PROJECTION_SAMPLE_RUN: Final = Table(
    "loop_projection_sample_run",
    METADATA,
    _uuid("organization_id", primary_key=True),
    _uuid("plant_id", primary_key=True),
    Column("sample_day", Date, primary_key=True, nullable=False),
    _text("seed"),
    _integer("population"),
    _integer("sample_size"),
    _instant("started_at"),
)
"""Semilla registrada de la muestra diaria de ``verify_loop_projection`` (TASK-306, LC-LAZ-04,
PAT-NUC-MAN-04, ``laz_0037``), de solo anexar."""
