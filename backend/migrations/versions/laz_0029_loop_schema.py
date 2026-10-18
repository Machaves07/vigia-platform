"""Esquema ``loop``: proyección del lazo, decisiones, acciones, asistente y configuración
(TASK-301, LC-LAZ-02; NFR-LAZ-07, 08, 22 y 34; PAT-LAZ-ESC-02 a 04, PAT-LAZ-SEG-03).

Revisión laz_0029, primer eslabón de U-04 (prefijo ``laz_``, nota U04-H-17). Las 19 tablas de
``domain-entities.md`` §2.1 a §2.14 con sus notas fechadas; los servicios (TASK-304 en adelante)
solo escriben repositorios encima. Ninguna tabla duplica el expediente: son proyecciones y estado
operativo, y todo hecho se escribe por ``EscritorExpediente``.

- Toda tabla lleva ``organization_id``. Las de zona referencian ``identity.zone (organization_id,
  plant_id, zone_id)`` y las de planta ``identity.plant``, así que la base garantiza que zona,
  planta y organización concuerdan (BR-NUC-08). ``action_evidence_entry`` y
  ``unclosable_review`` llevan además la planta y la zona de su acción o declaración, para que la
  política de proveedor las acote como al resto. Las marcas de tiempo no tienen valor por
  defecto: las pone la aplicación con su ``Clock``.
- **Seguridad a nivel de fila** (``migrations/laz_policies.py``): ``organization_isolation``
  forzada en las 19; ``provider_concession_scope`` RESTRICTIVE en toda tabla con planta; y
  ``provider_context_denied`` RESTRICTIVE (A-69) en las cinco de solo organización
  (``ORGANIZATION_ONLY_TABLES``): bajo cualquier concesión, ninguna fila visible ni escribible.
- **Particiones mensuales** (UTC, NFR-LAZ-07): ``finding_state`` por ``occurred_at_bucket`` (que
  nunca cambia), ``classification`` por ``classified_at`` y ``review_resolution`` por
  ``resolved_at``, con partición por defecto; esta migración crea el mes en curso y los tres
  siguientes. La tarea heredada ``create_partitions`` las mantiene con dos funciones **nuevas**,
  ``shared.vigia_create_loop_month_partitions`` y ``shared.vigia_loop_default_partition_rows``
  (mismo cuerpo, guarda de actor y candado que las de ``fleet``): las de nuc_0016 y gob_0018 no
  se tocan, porque la imagen N-1 convierte cada fila que devuelven a su ``PartitionedTable`` y
  una fila de ``loop`` haría fallar su ``create_partitions`` (NFR-NUC-14). Las particiones no se
  protegen como de solo anexar: son proyecciones que la reconstrucción reescribe (LC-LAZ-03).
  La clave primaria lleva la columna de partición (PostgreSQL no admite otra cosa).
- **Índices** (PAT-LAZ-ESC-03 y 02): ``finding_state_inbox`` parcial sobre los siete estados
  activos, ``finding_state_overdue`` parcial sobre ``action_assigned`` y ``overdue``, y
  ``assistant_request_rate`` por ``(organization_id, user_id, requested_at)`` para el límite
  exacto del asistente. El resto empieza por ``(organization_id, plant_id)`` (PAT-NUC-ESC-07).
- **Solo anexar** (``catalog.guard_update()`` de gob_0017, disparadores con ``ENABLE ALWAYS``):
  ``closure_attachment`` (cierre ``verified_at``) y ``assistant_request`` (los campos de la
  solicitud no cambian; ``state`` solo sale de ``pending``, ``resolved_at`` y
  ``unavailable_cause`` pasan una vez de nulo a valor y ``attempt_count`` es el contador de
  trabajo). ``DELETE`` y ``TRUNCATE`` fallan para cualquier rol.
- ``vigia_app``: ``SELECT`` e ``INSERT`` en todas; ``UPDATE`` solo sobre las columnas que el
  diseño deja cambiar (``app_updatable_columns``): nunca claves, organización, planta ni zona;
  nunca ``DELETE`` ni ``TRUNCATE``. Las decisiones (``classification``, ``review_resolution``,
  ``closure_record``, ``reopening``…) no tienen ninguna columna actualizable.
- **Retención** (NFR-LAZ-08): ``COMMENT ON TABLE`` declara cada tabla en exactamente una
  categoría (``RETENTION``); la metapropiedad está en
  ``tests/unit/test_laz_retention_categories.py``.

Lo que no hace: los esquemas ``accreditation`` y ``notifications`` (TASK-338 y TASK-339), la
lógica de dominio, el registro de tipos (TASK-302) ni subir ``MINIMUM_SCHEMA_VERSION``.
"""

from __future__ import annotations

from typing import Final

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0029"
down_revision: str | None = "gob_0028"
branch_labels: None = None
depends_on: None = None

SCHEMA: Final = "loop"
PARTITION_MONTHS_AHEAD: Final = 3
"""Meses siguientes al actual que la migración deja creados (PAT-NUC-ESC-01)."""

_HEX64 = "'^[0-9a-f]{64}$'"
_PREDICATE_FAMILY = "'coexistence', 'guard_bypass', 'dwell', 'startup_transition'"
_TIER = "'tier_1', 'tier_2'"
_OBSERVABILITY = "'observable', 'degraded', 'not_observable'"
_GATE_STATUS = "'pending', 'approved', 'revoked'"
_SUBJECT_KIND = "'finding', 'detection'"
_LIFECYCLE = (
    "'received', 'in_review', 'discarded', 'open', 'action_assigned', 'overdue', 'closed',"
    " 'unclosable', 'suspended_by_reclassification'"
)
"""``finding_lifecycle_state`` (§1)."""
ACTIVE_STATES: Final = (
    "received",
    "in_review",
    "open",
    "action_assigned",
    "overdue",
    "unclosable",
    "suspended_by_reclassification",
)
"""Los siete estados que la bandeja consulta a diario (NFR-LAZ-07, PAT-LAZ-ESC-03)."""
OVERDUE_STATES: Final = ("action_assigned", "overdue")
"""Los estados del panel de vencidos (PAT-LAZ-ESC-03)."""
_CLASSIFICATION_OUTCOME = "'confirmed', 'authorized_operation', 'false_positive'"
_REVIEW_OUTCOME = "'review_confirmed', 'review_discarded'"
_ACTION_TYPE = "'engineering', 'process'"
_CAUSE_CATEGORY = "'process', 'engineering', 'organization'"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _length(column: str, low: int, high: int) -> str:
    return f"CHECK (char_length({column}) BETWEEN {low} AND {high})"


def _object(column: str) -> str:
    return f"CHECK (jsonb_typeof({column}) = 'object')"


def _array(column: str, low: int, high: int) -> str:
    return (
        f"CHECK (CASE WHEN jsonb_typeof({column}) = 'array'"
        f" THEN jsonb_array_length({column}) BETWEEN {low} AND {high} ELSE false END)"
    )


def _signer(column: str) -> str:
    """Firma con nombre y licencia profesional (H-25): sin licencia no hay firma. Nula solo
    donde la columna lo admite (el análisis sin firmar)."""
    return (
        f"CHECK ({column} IS NULL OR (jsonb_typeof({column}) = 'object'"
        f" AND char_length(COALESCE({column} ->> 'professional_license', '')) BETWEEN 1 AND 64))"
    )


_TABLES = (
    # §2.1 Episode 🔁: la unidad de gestión; el identificador es el del nodo.
    f"""
    CREATE TABLE loop.episode (
        episode_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        family text NOT NULL
            CONSTRAINT episode_family_values CHECK (family IN ({_PREDICATE_FAMILY})),
        tier text NOT NULL CONSTRAINT episode_tier_values CHECK (tier IN ({_TIER})),
        standard_id uuid NOT NULL,
        standard_version integer NOT NULL
            CONSTRAINT episode_standard_version CHECK (standard_version >= 1),
        segment_record_ids uuid[] NOT NULL
            CONSTRAINT episode_segment_record_ids CHECK (
                cardinality(segment_record_ids) BETWEEN 1 AND 96
                AND array_ndims(segment_record_ids) = 1
                AND array_position(segment_record_ids, NULL) IS NULL
            ),
        segment_count integer NOT NULL CONSTRAINT episode_segment_count CHECK (segment_count >= 1),
        expected_segment_count integer NOT NULL
            CONSTRAINT episode_expected_segment_count CHECK (expected_segment_count >= 1),
        missing_segment_indexes integer[] NOT NULL
            CONSTRAINT episode_missing_segment_indexes CHECK (
                array_position(missing_segment_indexes, NULL) IS NULL
                AND cardinality(missing_segment_indexes) <= 96
            ),
        started_at timestamptz NOT NULL,
        ended_at timestamptz NOT NULL,
        total_episode_duration_ms bigint NOT NULL
            CONSTRAINT episode_total_episode_duration CHECK (total_episode_duration_ms >= 0),
        total_condition_duration_ms bigint NOT NULL
            CONSTRAINT episode_total_condition_duration CHECK (total_condition_duration_ms >= 0),
        max_confidence double precision NOT NULL
            CONSTRAINT episode_max_confidence CHECK (max_confidence BETWEEN 0 AND 1),
        clip_references jsonb NOT NULL
            CONSTRAINT episode_clip_references_array
                CHECK (jsonb_typeof(clip_references) = 'array'),
        observability_state_at_start text NOT NULL
            CONSTRAINT episode_observability_values
                CHECK (observability_state_at_start IN ({_OBSERVABILITY})),
        continues_open boolean NOT NULL,
        last_segment_received_at timestamptz NOT NULL,
        last_event_id uuid NOT NULL,
        projection_updated_at timestamptz NOT NULL,
        CONSTRAINT episode_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT episode_interval CHECK (ended_at >= started_at)
    )
    """,
    "CREATE INDEX episode_scope ON loop.episode (organization_id, plant_id, zone_id, started_at)",
    # §2.2 FindingState 🔁: la proyección de lectura de la bandeja, particionada por mes del cubo.
    f"""
    CREATE TABLE loop.finding_state (
        finding_state_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT finding_state_subject_kind_values CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        anchor_record_id uuid NOT NULL,
        family text NOT NULL
            CONSTRAINT finding_state_family_values CHECK (family IN ({_PREDICATE_FAMILY})),
        tier text NOT NULL CONSTRAINT finding_state_tier_values CHECK (tier IN ({_TIER})),
        standard_id uuid NOT NULL,
        standard_version integer NOT NULL
            CONSTRAINT finding_state_standard_version CHECK (standard_version >= 1),
        lifecycle_state text NOT NULL
            CONSTRAINT finding_state_lifecycle_values CHECK (lifecycle_state IN ({_LIFECYCLE})),
        previous_lifecycle_state text
            CONSTRAINT finding_state_previous_lifecycle_values
                CHECK (previous_lifecycle_state IN ({_LIFECYCLE})),
        current_classification_outcome text
            CONSTRAINT finding_state_classification_outcome_values
                CHECK (current_classification_outcome IN ({_CLASSIFICATION_OUTCOME})),
        current_reason_category text
            CONSTRAINT finding_state_reason_category_length
                {_length("current_reason_category", 1, 64)},
        classification_count integer NOT NULL DEFAULT 0
            CONSTRAINT finding_state_classification_count CHECK (classification_count >= 0),
        first_classified_at timestamptz,
        last_classified_at timestamptz,
        classified_within_deadline boolean,
        current_action_id uuid,
        current_action_type text
            CONSTRAINT finding_state_action_type_values
                CHECK (current_action_type IN ({_ACTION_TYPE})),
        assignee_user_id uuid REFERENCES identity.user_account (user_id),
        assignee_inactive boolean NOT NULL DEFAULT false,
        due_at timestamptz,
        overdue_since timestamptz,
        overdue_days integer NOT NULL DEFAULT 0
            CONSTRAINT finding_state_overdue_days CHECK (overdue_days >= 0),
        closed_at timestamptz,
        closure_record_id uuid,
        unclosable_since timestamptz,
        unclosable_review_due_at timestamptz,
        unclosable_pending_review boolean NOT NULL DEFAULT false,
        closed_with_later_different_classification boolean NOT NULL DEFAULT false,
        reopening_count integer NOT NULL DEFAULT 0
            CONSTRAINT finding_state_reopening_count CHECK (reopening_count >= 0),
        has_signed_root_cause boolean NOT NULL DEFAULT false,
        observability_state_at_event text NOT NULL
            CONSTRAINT finding_state_observability_values
                CHECK (observability_state_at_event IN ({_OBSERVABILITY})),
        zone_gate_status_at_event text NOT NULL
            CONSTRAINT finding_state_gate_status_values
                CHECK (zone_gate_status_at_event IN ({_GATE_STATUS})),
        zone_agreement_revoked boolean NOT NULL DEFAULT false,
        zone_agreement_revoked_at timestamptz,
        single_occupancy_zone boolean NOT NULL,
        aggregation_window_minutes integer NOT NULL DEFAULT 60
            CONSTRAINT finding_state_aggregation_window
                CHECK (aggregation_window_minutes BETWEEN 15 AND 480),
        walk_test_regression_pending boolean NOT NULL DEFAULT false,
        walk_test_regression_cause text
            CONSTRAINT finding_state_regression_cause_length
                {_length("walk_test_regression_cause", 1, 64)},
        occurred_at_exact timestamptz NOT NULL,
        occurred_at_bucket timestamptz NOT NULL,
        age_days integer NOT NULL DEFAULT 0 CONSTRAINT finding_state_age_days CHECK (age_days >= 0),
        evidence_flagged boolean NOT NULL DEFAULT false,
        missing_segments_declared boolean NOT NULL DEFAULT false,
        last_event_id uuid NOT NULL,
        last_record_id uuid NOT NULL,
        projection_updated_at timestamptz NOT NULL,
        projection_digest text NOT NULL
            CONSTRAINT finding_state_projection_digest_format CHECK (projection_digest ~ {_HEX64}),
        CONSTRAINT finding_state_pkey PRIMARY KEY (finding_state_id, occurred_at_bucket),
        CONSTRAINT finding_state_subject
            UNIQUE (organization_id, subject_kind, subject_id, occurred_at_bucket),
        CONSTRAINT finding_state_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT finding_state_previous_only_when_suspended CHECK (
            (lifecycle_state = 'suspended_by_reclassification')
                = (previous_lifecycle_state IS NOT NULL)
        ),
        CONSTRAINT finding_state_classified_iff_counted
            CHECK ((classification_count = 0) = (current_classification_outcome IS NULL)),
        CONSTRAINT finding_state_bucket_not_after_exact
            CHECK (occurred_at_bucket <= occurred_at_exact)
    ) PARTITION BY RANGE (occurred_at_bucket)
    """,
    f"""
    CREATE INDEX finding_state_inbox ON loop.finding_state
        (organization_id, plant_id, zone_id, lifecycle_state, occurred_at_bucket DESC,
         finding_state_id)
        WHERE lifecycle_state IN ({_in(ACTIVE_STATES)})
    """,
    f"""
    CREATE INDEX finding_state_overdue ON loop.finding_state
        (organization_id, plant_id, due_at, finding_state_id)
        WHERE lifecycle_state IN ({_in(OVERDUE_STATES)})
    """,
    """
    CREATE INDEX finding_state_scope_subject
        ON loop.finding_state (organization_id, plant_id, zone_id, subject_id)
    """,
    # §2.3 Classification 🔁: el historial de decisiones; ninguna fila se edita (H-22, P4).
    f"""
    CREATE TABLE loop.classification (
        classification_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT classification_subject_kind_values CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        record_id uuid NOT NULL,
        sequence_in_subject integer NOT NULL
            CONSTRAINT classification_sequence CHECK (sequence_in_subject >= 1),
        outcome text NOT NULL
            CONSTRAINT classification_outcome_values CHECK (outcome IN ({_CLASSIFICATION_OUTCOME})),
        reason_category text NOT NULL
            CONSTRAINT classification_reason_category_length {_length("reason_category", 1, 64)},
        reason_text text NOT NULL
            CONSTRAINT classification_reason_text_length {_length("reason_text", 20, 1000)},
        is_reclassification boolean NOT NULL,
        reinforced_reason boolean NOT NULL,
        previous_outcome text
            CONSTRAINT classification_previous_outcome_values
                CHECK (previous_outcome IN ({_CLASSIFICATION_OUTCOME})),
        effect_on_loop text NOT NULL
            CONSTRAINT classification_effect_values CHECK (effect_on_loop IN (
                'none', 'loop_opened', 'loop_suspended', 'loop_resumed', 'closed_marked'
            )),
        classified_by jsonb NOT NULL CONSTRAINT classification_classified_by_object
            {_object("classified_by")},
        classified_at timestamptz NOT NULL,
        label_id uuid NOT NULL,
        correlation_id uuid NOT NULL,
        CONSTRAINT classification_pkey PRIMARY KEY (classification_id, classified_at),
        CONSTRAINT classification_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT classification_reclassification_iff_sequence
            CHECK (is_reclassification = (sequence_in_subject > 1)),
        CONSTRAINT classification_previous_iff_reclassification
            CHECK (is_reclassification = (previous_outcome IS NOT NULL)),
        CONSTRAINT classification_reclassification_reinforced
            CHECK (NOT is_reclassification OR reinforced_reason)
    ) PARTITION BY RANGE (classified_at)
    """,
    """
    CREATE INDEX classification_scope_subject
        ON loop.classification (organization_id, plant_id, zone_id, subject_id, sequence_in_subject)
    """,
    # §2.4 ReviewQueueItem 🔁: nunca sale de la cola sin resolución (RF-PLA-03).
    f"""
    CREATE TABLE loop.review_queue_item (
        detection_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        anchor_record_id uuid NOT NULL,
        family text NOT NULL
            CONSTRAINT review_queue_item_family_values CHECK (family IN ({_PREDICATE_FAMILY})),
        standard_id uuid NOT NULL,
        standard_version integer NOT NULL
            CONSTRAINT review_queue_item_standard_version CHECK (standard_version >= 1),
        max_confidence double precision NOT NULL
            CONSTRAINT review_queue_item_max_confidence CHECK (max_confidence BETWEEN 0 AND 1),
        review_threshold double precision NOT NULL
            CONSTRAINT review_queue_item_review_threshold CHECK (review_threshold BETWEEN 0 AND 1),
        publication_threshold double precision NOT NULL
            CONSTRAINT review_queue_item_publication_threshold
                CHECK (publication_threshold BETWEEN 0 AND 1),
        review_reason text NOT NULL
            CONSTRAINT review_queue_item_review_reason_values CHECK (
                review_reason IN ('low_confidence', 'ambiguous', 'no_bounding_box', 'other')
            ),
        observability_state_at_event text NOT NULL
            CONSTRAINT review_queue_item_observability_values
                CHECK (observability_state_at_event IN ({_OBSERVABILITY})),
        occurred_at_exact timestamptz NOT NULL,
        occurred_at_bucket timestamptz NOT NULL,
        clip_references jsonb NOT NULL
            CONSTRAINT review_queue_item_clip_references_array
                CHECK (jsonb_typeof(clip_references) = 'array'),
        signals_summary jsonb NOT NULL
            CONSTRAINT review_queue_item_signals_summary_object {_object("signals_summary")},
        received_at timestamptz NOT NULL,
        review_due_at timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'pending'
            CONSTRAINT review_queue_item_state_values CHECK (state IN ('pending', 'resolved')),
        resolved_at timestamptz,
        reminder_sent_at timestamptz,
        reminders_sent integer NOT NULL DEFAULT 0
            CONSTRAINT review_queue_item_reminders_sent CHECK (reminders_sent >= 0),
        source_event_id uuid NOT NULL,
        CONSTRAINT review_queue_item_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT review_queue_item_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT review_queue_item_source_event UNIQUE (organization_id, source_event_id),
        CONSTRAINT review_queue_item_thresholds
            CHECK (review_threshold <= publication_threshold),
        CONSTRAINT review_queue_item_resolved_has_date
            CHECK ((state = 'resolved') = (resolved_at IS NOT NULL)),
        CONSTRAINT review_queue_item_due_after_received CHECK (review_due_at > received_at),
        CONSTRAINT review_queue_item_bucket_not_after_exact
            CHECK (occurred_at_bucket <= occurred_at_exact)
    )
    """,
    """
    CREATE INDEX review_queue_item_pending
        ON loop.review_queue_item (organization_id, plant_id, zone_id, review_due_at, detection_id)
        WHERE state = 'pending'
    """,
    # §2.4 ReviewResolution 🔁, particionada por mes de resolved_at.
    f"""
    CREATE TABLE loop.review_resolution (
        resolution_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        detection_id uuid NOT NULL,
        record_id uuid NOT NULL,
        outcome text NOT NULL
            CONSTRAINT review_resolution_outcome_values CHECK (outcome IN ({_REVIEW_OUTCOME})),
        reason_category text NOT NULL
            CONSTRAINT review_resolution_reason_category_length
                {_length("reason_category", 1, 64)},
        reason_text text NOT NULL
            CONSTRAINT review_resolution_reason_text_length {_length("reason_text", 20, 1000)},
        resolved_by jsonb NOT NULL
            CONSTRAINT review_resolution_resolved_by_object {_object("resolved_by")},
        resolved_at timestamptz NOT NULL,
        label_id uuid NOT NULL,
        correlation_id uuid NOT NULL,
        CONSTRAINT review_resolution_pkey PRIMARY KEY (resolution_id, resolved_at),
        CONSTRAINT review_resolution_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id)
    ) PARTITION BY RANGE (resolved_at)
    """,
    """
    CREATE INDEX review_resolution_scope_detection
        ON loop.review_resolution (organization_id, plant_id, zone_id, detection_id)
    """,
    # §2.5 CorrectiveAction 🔁: sobre la zona, nunca sobre una persona (H-24, P7).
    f"""
    CREATE TABLE loop.corrective_action (
        action_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT corrective_action_subject_kind_values
                CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        record_id uuid NOT NULL,
        action_type text NOT NULL
            CONSTRAINT corrective_action_type_values CHECK (action_type IN ({_ACTION_TYPE})),
        action_description text NOT NULL
            CONSTRAINT corrective_action_description_length
                {_length("action_description", 20, 2000)},
        expected_outcome text
            CONSTRAINT corrective_action_expected_outcome_length
                {_length("expected_outcome", 0, 1000)},
        assignee_user_id uuid NOT NULL REFERENCES identity.user_account (user_id),
        assigned_by jsonb NOT NULL
            CONSTRAINT corrective_action_assigned_by_object {_object("assigned_by")},
        assigned_at timestamptz NOT NULL,
        due_at timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'assigned'
            CONSTRAINT corrective_action_state_values
                CHECK (state IN ('assigned', 'overdue', 'closed', 'superseded')),
        root_cause_analysis_id uuid,
        counts_as_overdue boolean NOT NULL DEFAULT true,
        CONSTRAINT corrective_action_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT corrective_action_scope UNIQUE (organization_id, plant_id, action_id),
        CONSTRAINT corrective_action_due_after_assignment CHECK (due_at > assigned_at)
    )
    """,
    """
    CREATE INDEX corrective_action_scope_subject
        ON loop.corrective_action (organization_id, plant_id, zone_id, subject_id)
    """,
    # §2.6 ActionEvidenceEntry 🔁: descripción estructurada obligatoria y hasta 5 adjuntos.
    f"""
    CREATE TABLE loop.action_evidence_entry (
        evidence_entry_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        action_id uuid NOT NULL,
        record_id uuid NOT NULL,
        description text NOT NULL
            CONSTRAINT action_evidence_entry_description_length
                {_length("description", 20, 2000)},
        verification_note text
            CONSTRAINT action_evidence_entry_verification_note_length
                {_length("verification_note", 0, 1000)},
        performed_at timestamptz NOT NULL,
        performed_by_role text NOT NULL
            CONSTRAINT action_evidence_entry_performed_by_role_length
                {_length("performed_by_role", 1, 64)},
        attachment_ids uuid[] NOT NULL
            CONSTRAINT action_evidence_entry_attachment_ids CHECK (
                cardinality(attachment_ids) <= 5
                AND array_position(attachment_ids, NULL) IS NULL
            ),
        added_by jsonb NOT NULL
            CONSTRAINT action_evidence_entry_added_by_object {_object("added_by")},
        added_at timestamptz NOT NULL,
        CONSTRAINT action_evidence_entry_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT action_evidence_entry_action_fkey
            FOREIGN KEY (organization_id, plant_id, action_id)
            REFERENCES loop.corrective_action (organization_id, plant_id, action_id)
    )
    """,
    """
    CREATE INDEX action_evidence_entry_scope_action
        ON loop.action_evidence_entry (organization_id, plant_id, action_id)
    """,
    # §2.6 y §6.3 ClosureEvidenceAttachment ⛓: clave disjunta de la de los clips, hasta 10 MB y
    # la declaración firmada de que no hay persona identificable (constante true).
    """
    CREATE TABLE loop.closure_attachment (
        attachment_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_id uuid NOT NULL,
        content_type text NOT NULL
            CONSTRAINT closure_attachment_content_type_values
                CHECK (content_type IN ('image/jpeg', 'image/png', 'application/pdf')),
        size_bytes bigint NOT NULL
            CONSTRAINT closure_attachment_size CHECK (size_bytes BETWEEN 1 AND 10485760),
        sha256 text NOT NULL
            CONSTRAINT closure_attachment_sha256_format CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        filename text NOT NULL
            CONSTRAINT closure_attachment_filename CHECK (
                char_length(filename) BETWEEN 1 AND 128 AND filename !~ '[/\\\\]'
            ),
        storage_key text NOT NULL
            CONSTRAINT closure_attachment_storage_key_format CHECK (
                char_length(storage_key) <= 512
                AND storage_key ~ (
                    '^org/' || organization_id::text || '/plant/' || plant_id::text
                    || '/zone/' || zone_id::text || '/closure/' || subject_id::text
                    || '/' || attachment_id::text || '[.](jpg|jpeg|png|pdf)$'
                )
            ),
        no_identifiable_person_declared boolean NOT NULL
            CONSTRAINT closure_attachment_declared CHECK (no_identifiable_person_declared),
        declared_by jsonb NOT NULL
            CONSTRAINT closure_attachment_declared_by_object
                CHECK (jsonb_typeof(declared_by) = 'object'),
        uploaded_at timestamptz NOT NULL,
        verified_at timestamptz,
        CONSTRAINT closure_attachment_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT closure_attachment_verified_after_upload CHECK (verified_at >= uploaded_at)
    )
    """,
    """
    CREATE INDEX closure_attachment_scope_subject
        ON loop.closure_attachment (organization_id, plant_id, zone_id, subject_id)
    """,
    # §2.6 ClosureRecord 🔁: no existe cierre sin evidencia ni sin licencia (H-25, P5).
    f"""
    CREATE TABLE loop.closure_record (
        closure_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT closure_record_subject_kind_values CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        action_id uuid NOT NULL,
        record_id uuid NOT NULL,
        closure_description text NOT NULL
            CONSTRAINT closure_record_description_length
                {_length("closure_description", 20, 2000)},
        verification_note text
            CONSTRAINT closure_record_verification_note_length
                {_length("verification_note", 0, 1000)},
        evidence_entry_ids uuid[] NOT NULL
            CONSTRAINT closure_record_evidence_entry_ids CHECK (
                cardinality(evidence_entry_ids) >= 1
                AND array_position(evidence_entry_ids, NULL) IS NULL
            ),
        attachment_digests text[] NOT NULL
            CONSTRAINT closure_record_attachment_digests CHECK (
                array_position(attachment_digests, NULL) IS NULL
                AND array_to_string(attachment_digests, ',') ~ '^([0-9a-f]{{64}}(,|$))*$'
            ),
        signer jsonb NOT NULL CONSTRAINT closure_record_signer {_signer("signer")},
        signed_at timestamptz NOT NULL,
        correlation_id uuid NOT NULL,
        CONSTRAINT closure_record_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT closure_record_action_fkey FOREIGN KEY (organization_id, plant_id, action_id)
            REFERENCES loop.corrective_action (organization_id, plant_id, action_id)
    )
    """,
    """
    CREATE INDEX closure_record_scope_subject
        ON loop.closure_record (organization_id, plant_id, zone_id, subject_id)
    """,
    # §2.7 UnclosableDeclaration 🔁: los cinco campos a la vez (H13, H-26).
    f"""
    CREATE TABLE loop.unclosable_declaration (
        declaration_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT unclosable_declaration_subject_kind_values
                CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        record_id uuid NOT NULL,
        evaluated_alternative text NOT NULL
            CONSTRAINT unclosable_declaration_evaluated_alternative_length
                {_length("evaluated_alternative", 20, 2000)},
        rejection_reason text NOT NULL
            CONSTRAINT unclosable_declaration_rejection_reason_length
                {_length("rejection_reason", 20, 2000)},
        responsible_user_id uuid NOT NULL REFERENCES identity.user_account (user_id),
        decision_date date NOT NULL,
        compensatory_measure text NOT NULL
            CONSTRAINT unclosable_declaration_compensatory_measure_length
                {_length("compensatory_measure", 20, 2000)},
        review_due_at timestamptz NOT NULL,
        policy_id uuid NOT NULL,
        policy_version text NOT NULL
            CONSTRAINT unclosable_declaration_policy_version_length
                {_length("policy_version", 1, 32)},
        policy_document_sha256 text NOT NULL
            CONSTRAINT unclosable_declaration_policy_sha256_format
                CHECK (policy_document_sha256 ~ {_HEX64}),
        signer jsonb NOT NULL CONSTRAINT unclosable_declaration_signer {_signer("signer")},
        declared_at timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'active'
            CONSTRAINT unclosable_declaration_state_values
                CHECK (state IN ('active', 'pending_review', 'renewed', 'converted')),
        CONSTRAINT unclosable_declaration_zone_fkey
            FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT unclosable_declaration_scope UNIQUE (organization_id, plant_id, declaration_id),
        CONSTRAINT unclosable_declaration_review_after_declaration
            CHECK (review_due_at > declared_at)
    )
    """,
    """
    CREATE INDEX unclosable_declaration_scope_review
        ON loop.unclosable_declaration (organization_id, plant_id, state, review_due_at)
    """,
    # §2.7 UnclosableReview 🔁, con la planta y la zona de su declaración.
    f"""
    CREATE TABLE loop.unclosable_review (
        review_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        declaration_id uuid NOT NULL,
        record_id uuid NOT NULL,
        outcome text NOT NULL
            CONSTRAINT unclosable_review_outcome_values
                CHECK (outcome IN ('renewed', 'converted_to_action')),
        review_note text NOT NULL
            CONSTRAINT unclosable_review_note_length {_length("review_note", 20, 2000)},
        new_review_due_at timestamptz,
        new_action_id uuid,
        reviewed_by jsonb NOT NULL
            CONSTRAINT unclosable_review_reviewed_by {_signer("reviewed_by")},
        reviewed_at timestamptz NOT NULL,
        CONSTRAINT unclosable_review_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT unclosable_review_declaration_fkey
            FOREIGN KEY (organization_id, plant_id, declaration_id)
            REFERENCES loop.unclosable_declaration (organization_id, plant_id, declaration_id),
        CONSTRAINT unclosable_review_renewed_has_date
            CHECK ((outcome = 'renewed') = (new_review_due_at IS NOT NULL)),
        CONSTRAINT unclosable_review_converted_has_action
            CHECK ((outcome = 'converted_to_action') = (new_action_id IS NOT NULL))
    )
    """,
    """
    CREATE INDEX unclosable_review_scope_declaration
        ON loop.unclosable_review (organization_id, plant_id, declaration_id)
    """,
    # §2.8 Reopening 🔁: un registro nuevo, nunca una corrección del anterior (P4).
    f"""
    CREATE TABLE loop.reopening (
        reopening_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT reopening_subject_kind_values CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        record_id uuid NOT NULL,
        from_state text NOT NULL
            CONSTRAINT reopening_from_state_values CHECK (from_state IN ('closed', 'unclosable')),
        previous_closure_id uuid,
        previous_declaration_id uuid,
        reason_text text NOT NULL
            CONSTRAINT reopening_reason_text_length {_length("reason_text", 20, 2000)},
        new_action_id uuid NOT NULL,
        reopened_by jsonb NOT NULL CONSTRAINT reopening_reopened_by_object {_object("reopened_by")},
        reopened_at timestamptz NOT NULL,
        CONSTRAINT reopening_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT reopening_previous_matches_state CHECK (
            (from_state = 'closed' AND previous_declaration_id IS NULL)
            OR (from_state = 'unclosable' AND previous_closure_id IS NULL)
        )
    )
    """,
    """
    CREATE INDEX reopening_scope_subject
        ON loop.reopening (organization_id, plant_id, zone_id, subject_id)
    """,
    # §2.9 RootCauseAnalysis 🔁: borrador, pendiente de información o firmado (con licencia).
    f"""
    CREATE TABLE loop.root_cause_analysis (
        analysis_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        subject_kind text NOT NULL
            CONSTRAINT root_cause_analysis_subject_kind_values
                CHECK (subject_kind IN ({_SUBJECT_KIND})),
        subject_id uuid NOT NULL,
        record_id uuid,
        state text NOT NULL DEFAULT 'draft'
            CONSTRAINT root_cause_analysis_state_values
                CHECK (state IN ('draft', 'pending_information', 'signed')),
        zone_context jsonb NOT NULL
            CONSTRAINT root_cause_analysis_zone_context_object {_object("zone_context")},
        why_steps jsonb NOT NULL
            CONSTRAINT root_cause_analysis_why_steps {_array("why_steps", 0, 5)},
        cause_effect_branches jsonb NOT NULL
            CONSTRAINT root_cause_analysis_branches {_array("cause_effect_branches", 0, 6)},
        root_cause_category text
            CONSTRAINT root_cause_analysis_category_values
                CHECK (root_cause_category IN ({_CAUSE_CATEGORY})),
        root_cause_statement text
            CONSTRAINT root_cause_analysis_statement_length
                {_length("root_cause_statement", 20, 2000)},
        conclusion text
            CONSTRAINT root_cause_analysis_conclusion_length {_length("conclusion", 20, 2000)},
        missing_information text
            CONSTRAINT root_cause_analysis_missing_information_length
                {_length("missing_information", 0, 1000)},
        assistant_used boolean NOT NULL DEFAULT false,
        assistant_model text
            CONSTRAINT root_cause_analysis_assistant_model_length
                {_length("assistant_model", 1, 128)},
        assistant_version text
            CONSTRAINT root_cause_analysis_assistant_version_length
                {_length("assistant_version", 1, 64)},
        assistant_text_edited boolean,
        signer jsonb CONSTRAINT root_cause_analysis_signer {_signer("signer")},
        signed_at timestamptz,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL,
        CONSTRAINT root_cause_analysis_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT root_cause_analysis_scope UNIQUE (organization_id, plant_id, analysis_id),
        CONSTRAINT root_cause_analysis_signature_complete CHECK (
            (state = 'signed') = (record_id IS NOT NULL)
            AND (state = 'signed') = (signer IS NOT NULL)
            AND (state = 'signed') = (signed_at IS NOT NULL)
        ),
        CONSTRAINT root_cause_analysis_signed_is_complete CHECK (
            state <> 'signed'
            OR (
                root_cause_category IS NOT NULL
                AND root_cause_statement IS NOT NULL
                AND conclusion IS NOT NULL
                AND jsonb_array_length(why_steps) >= 1
            )
        ),
        CONSTRAINT root_cause_analysis_pending_has_missing
            CHECK (state <> 'pending_information' OR missing_information IS NOT NULL),
        CONSTRAINT root_cause_analysis_order
            CHECK (updated_at >= created_at AND signed_at >= created_at)
    )
    """,
    """
    CREATE INDEX root_cause_analysis_scope_subject
        ON loop.root_cause_analysis (organization_id, plant_id, zone_id, subject_id)
    """,
    # §2.11 AssistantRequest ⛓ (fuera del expediente, P8): los campos de la solicitud no cambian.
    f"""
    CREATE TABLE loop.assistant_request (
        request_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        analysis_id uuid NOT NULL,
        user_id uuid NOT NULL REFERENCES identity.user_account (user_id),
        requested_by jsonb NOT NULL
            CONSTRAINT assistant_request_requested_by_object {_object("requested_by")},
        requested_at timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'pending'
            CONSTRAINT assistant_request_state_values CHECK (
                state IN ('pending', 'ready', 'unavailable', 'rejected_by_validators')
            ),
        unavailable_cause text
            CONSTRAINT assistant_request_unavailable_cause_values CHECK (
                unavailable_cause IN ('no_api_key', 'circuit_open', 'timeout', 'provider_error')
            ),
        resolved_at timestamptz,
        outbox_event_id uuid NOT NULL,
        attempt_count integer NOT NULL DEFAULT 0
            CONSTRAINT assistant_request_attempt_count CHECK (attempt_count >= 0),
        context_digest text NOT NULL
            CONSTRAINT assistant_request_context_digest_format CHECK (context_digest ~ {_HEX64}),
        CONSTRAINT assistant_request_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT assistant_request_analysis_fkey
            FOREIGN KEY (organization_id, plant_id, analysis_id)
            REFERENCES loop.root_cause_analysis (organization_id, plant_id, analysis_id),
        CONSTRAINT assistant_request_scope UNIQUE (organization_id, request_id),
        CONSTRAINT assistant_request_cause_iff_unavailable
            CHECK ((state = 'unavailable') = (unavailable_cause IS NOT NULL)),
        CONSTRAINT assistant_request_resolved_iff_not_pending
            CHECK ((state = 'pending') = (resolved_at IS NULL)),
        CONSTRAINT assistant_request_resolved_after_request CHECK (resolved_at >= requested_at)
    )
    """,
    """
    CREATE INDEX assistant_request_rate
        ON loop.assistant_request (organization_id, user_id, requested_at)
    """,
    """
    CREATE INDEX assistant_request_scope_analysis
        ON loop.assistant_request (organization_id, plant_id, analysis_id)
    """,
    # §2.11 AssistantDraft: un borrador nunca firmado no entra al expediente (P8).
    f"""
    CREATE TABLE loop.assistant_draft (
        draft_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        request_id uuid NOT NULL,
        draft_text text NOT NULL
            CONSTRAINT assistant_draft_text_length {_length("draft_text", 1, 8000)},
        citations jsonb NOT NULL
            CONSTRAINT assistant_draft_citations {_array("citations", 1, 100)},
        insufficient_evidence_declared boolean NOT NULL,
        model_name text NOT NULL
            CONSTRAINT assistant_draft_model_name_length {_length("model_name", 1, 128)},
        model_version text NOT NULL
            CONSTRAINT assistant_draft_model_version_length {_length("model_version", 1, 64)},
        provider text NOT NULL
            CONSTRAINT assistant_draft_provider_constant CHECK (provider = 'anthropic'),
        validation_results jsonb NOT NULL
            CONSTRAINT assistant_draft_validation_results_array
                CHECK (jsonb_typeof(validation_results) = 'array'),
        accepted_by_human boolean NOT NULL DEFAULT false,
        edited_by_human boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL,
        CONSTRAINT assistant_draft_request_fkey FOREIGN KEY (organization_id, request_id)
            REFERENCES loop.assistant_request (organization_id, request_id)
    )
    """,
    "CREATE INDEX assistant_draft_scope ON loop.assistant_draft (organization_id, request_id)",
    # §2.12 BlockedVocabularyList 🔁: una lista vigente por organización, versión monótona.
    """
    CREATE TABLE loop.blocked_vocabulary_list (
        list_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        version integer NOT NULL CONSTRAINT blocked_vocabulary_list_version CHECK (version >= 1),
        effective_from timestamptz NOT NULL,
        created_by jsonb NOT NULL
            CONSTRAINT blocked_vocabulary_list_created_by_object
                CHECK (jsonb_typeof(created_by) = 'object'),
        record_id uuid NOT NULL,
        lemmatizer_version text
            CONSTRAINT blocked_vocabulary_list_lemmatizer_version_length
                CHECK (char_length(lemmatizer_version) BETWEEN 1 AND 64),
        CONSTRAINT blocked_vocabulary_list_one_per_organization UNIQUE (organization_id),
        CONSTRAINT blocked_vocabulary_list_scope UNIQUE (organization_id, list_id)
    )
    """,
    # §2.12 BlockedTerm 🔁: un término vigente no se repite en la organización.
    f"""
    CREATE TABLE loop.blocked_term (
        term_id uuid PRIMARY KEY,
        list_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        category text NOT NULL
            CONSTRAINT blocked_term_category_values CHECK (category IN (
                'absence_claim', 'percentage_guarantee', 'reduction_promise',
                'intent_or_blame_attribution'
            )),
        form text NOT NULL CONSTRAINT blocked_term_form_values CHECK (form IN ('lemma', 'pattern')),
        value text NOT NULL,
        suggestion_es text NOT NULL
            CONSTRAINT blocked_term_suggestion_length {_length("suggestion_es", 1, 200)},
        origin text NOT NULL
            CONSTRAINT blocked_term_origin_values CHECK (origin IN ('derived', 'customer_added')),
        added_in_version integer NOT NULL
            CONSTRAINT blocked_term_added_in_version CHECK (added_in_version >= 1),
        removed_in_version integer,
        added_reason text NOT NULL
            CONSTRAINT blocked_term_added_reason_length {_length("added_reason", 10, 500)},
        CONSTRAINT blocked_term_list_fkey FOREIGN KEY (organization_id, list_id)
            REFERENCES loop.blocked_vocabulary_list (organization_id, list_id),
        CONSTRAINT blocked_term_value_length CHECK (
            char_length(value) >= 1
            AND char_length(value) <= CASE form WHEN 'lemma' THEN 64 ELSE 256 END
        ),
        CONSTRAINT blocked_term_removed_after_added
            CHECK (removed_in_version > added_in_version)
    )
    """,
    """
    CREATE UNIQUE INDEX blocked_term_one_current
        ON loop.blocked_term (organization_id, form, value) WHERE removed_in_version IS NULL
    """,
    "CREATE INDEX blocked_term_scope ON loop.blocked_term (organization_id, list_id)",
    # §2.13 PlantLoopConfiguration 🔁 con reminder_interval_hours (U04-H-07).
    """
    CREATE TABLE loop.plant_loop_configuration (
        plant_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        classification_deadline_business_days integer NOT NULL DEFAULT 3
            CONSTRAINT plant_loop_configuration_classification_deadline
                CHECK (classification_deadline_business_days BETWEEN 1 AND 30),
        review_deadline_business_days integer NOT NULL DEFAULT 5
            CONSTRAINT plant_loop_configuration_review_deadline
                CHECK (review_deadline_business_days BETWEEN 1 AND 30),
        action_deadline_default_days integer NOT NULL DEFAULT 30
            CONSTRAINT plant_loop_configuration_action_deadline_default
                CHECK (action_deadline_default_days BETWEEN 1 AND 180),
        action_deadline_min_days integer NOT NULL DEFAULT 1
            CONSTRAINT plant_loop_configuration_action_deadline_min
                CHECK (action_deadline_min_days BETWEEN 1 AND 180),
        action_deadline_max_days integer NOT NULL DEFAULT 180
            CONSTRAINT plant_loop_configuration_action_deadline_max
                CHECK (action_deadline_max_days BETWEEN 1 AND 180),
        manager_highlight_overdue_days integer NOT NULL DEFAULT 30
            CONSTRAINT plant_loop_configuration_manager_highlight
                CHECK (manager_highlight_overdue_days >= 1),
        unclosable_review_default_months integer NOT NULL DEFAULT 6
            CONSTRAINT plant_loop_configuration_unclosable_default
                CHECK (unclosable_review_default_months BETWEEN 1 AND 12),
        unclosable_review_max_months integer NOT NULL DEFAULT 12
            CONSTRAINT plant_loop_configuration_unclosable_max
                CHECK (unclosable_review_max_months BETWEEN 1 AND 12),
        monthly_package_generation_day integer NOT NULL DEFAULT 1
            CONSTRAINT plant_loop_configuration_package_day
                CHECK (monthly_package_generation_day BETWEEN 1 AND 5),
        sample_percent integer NOT NULL DEFAULT 10
            CONSTRAINT plant_loop_configuration_sample_percent
                CHECK (sample_percent BETWEEN 1 AND 100),
        sample_min integer NOT NULL DEFAULT 5
            CONSTRAINT plant_loop_configuration_sample_min CHECK (sample_min >= 1),
        sample_max integer NOT NULL DEFAULT 30
            CONSTRAINT plant_loop_configuration_sample_max CHECK (sample_max >= 1),
        adoption_silence_days integer NOT NULL DEFAULT 14
            CONSTRAINT plant_loop_configuration_adoption_silence
                CHECK (adoption_silence_days BETWEEN 1 AND 90),
        exposure_coverage_threshold_percent integer NOT NULL DEFAULT 80
            CONSTRAINT plant_loop_configuration_exposure_threshold
                CHECK (exposure_coverage_threshold_percent BETWEEN 0 AND 100),
        email_grouping_hours integer NOT NULL DEFAULT 4
            CONSTRAINT plant_loop_configuration_email_grouping
                CHECK (email_grouping_hours BETWEEN 1 AND 24),
        export_max_days integer NOT NULL DEFAULT 366
            CONSTRAINT plant_loop_configuration_export_max_days CHECK (export_max_days >= 1),
        export_max_zones integer NOT NULL DEFAULT 20
            CONSTRAINT plant_loop_configuration_export_max_zones CHECK (export_max_zones >= 1),
        single_occupancy_default_window_minutes integer NOT NULL DEFAULT 60
            CONSTRAINT plant_loop_configuration_single_occupancy_window
                CHECK (single_occupancy_default_window_minutes BETWEEN 15 AND 480),
        reminder_interval_hours integer NOT NULL DEFAULT 24
            CONSTRAINT plant_loop_configuration_reminder_interval
                CHECK (reminder_interval_hours >= 1),
        business_calendar jsonb NOT NULL
            CONSTRAINT plant_loop_configuration_business_calendar CHECK (
                jsonb_typeof(business_calendar) = 'object'
                AND jsonb_typeof(business_calendar -> 'working_weekdays') = 'array'
                AND jsonb_typeof(business_calendar -> 'non_working_dates') = 'array'
            ),
        timezone text NOT NULL
            CONSTRAINT plant_loop_configuration_timezone_length
                CHECK (char_length(timezone) BETWEEN 1 AND 64),
        version integer NOT NULL CONSTRAINT plant_loop_configuration_version CHECK (version >= 1),
        changed_at timestamptz NOT NULL,
        changed_by jsonb NOT NULL
            CONSTRAINT plant_loop_configuration_changed_by_object
                CHECK (jsonb_typeof(changed_by) = 'object'),
        change_reason text NOT NULL
            CONSTRAINT plant_loop_configuration_change_reason_length
                CHECK (char_length(change_reason) BETWEEN 1 AND 1000),
        CONSTRAINT plant_loop_configuration_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT plant_loop_configuration_scope UNIQUE (organization_id, plant_id),
        CONSTRAINT plant_loop_configuration_action_deadlines CHECK (
            action_deadline_min_days <= action_deadline_default_days
            AND action_deadline_default_days <= action_deadline_max_days
        ),
        CONSTRAINT plant_loop_configuration_unclosable_months
            CHECK (unclosable_review_default_months <= unclosable_review_max_months),
        CONSTRAINT plant_loop_configuration_sample_bounds CHECK (sample_min <= sample_max)
    )
    """,
    # §2.14 LoopProjectionCheckpoint: el control de la proyección por (organización, planta).
    f"""
    CREATE TABLE loop.loop_projection_checkpoint (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        last_applied_ledger_sequence bigint NOT NULL DEFAULT 0
            CONSTRAINT loop_projection_checkpoint_sequence
                CHECK (last_applied_ledger_sequence >= 0),
        last_applied_event_id uuid,
        projection_digest text
            CONSTRAINT loop_projection_checkpoint_digest_format
                CHECK (projection_digest ~ {_HEX64}),
        rebuilt_at timestamptz,
        last_verified_at timestamptz,
        divergence_count integer NOT NULL DEFAULT 0
            CONSTRAINT loop_projection_checkpoint_divergence_count CHECK (divergence_count >= 0),
        last_divergence_at timestamptz,
        state text NOT NULL DEFAULT 'healthy'
            CONSTRAINT loop_projection_checkpoint_state_values
                CHECK (state IN ('healthy', 'rebuilding', 'diverged')),
        CONSTRAINT loop_projection_checkpoint_pkey PRIMARY KEY (organization_id, plant_id),
        CONSTRAINT loop_projection_checkpoint_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id)
    )
    """,
)

TABLES: Final = (
    "episode",
    "finding_state",
    "classification",
    "review_queue_item",
    "review_resolution",
    "corrective_action",
    "action_evidence_entry",
    "closure_attachment",
    "closure_record",
    "unclosable_declaration",
    "unclosable_review",
    "reopening",
    "root_cause_analysis",
    "assistant_request",
    "assistant_draft",
    "blocked_vocabulary_list",
    "blocked_term",
    "plant_loop_configuration",
    "loop_projection_checkpoint",
)
"""Las 19 tablas de ``loop`` (``domain-entities.md`` §2.1 a §2.14)."""

ORGANIZATION_ONLY_TABLES: Final = (
    "assistant_request",
    "assistant_draft",
    "blocked_vocabulary_list",
    "blocked_term",
    "loop_projection_checkpoint",
)
"""Las de solo organización según A-69: ninguna fila bajo una concesión de proveedor."""

PLANT_TABLES: Final = tuple(
    table
    for table in TABLES
    if table not in ("assistant_draft", "blocked_vocabulary_list", "blocked_term")
)
"""Las que llevan ``plant_id``: ``provider_concession_scope`` acota la concesión por planta."""

PARTITIONED_TABLES: Final = {
    "finding_state": "occurred_at_bucket",
    "classification": "classified_at",
    "review_resolution": "resolved_at",
}
"""Tablas particionadas por mes (UTC) y su columna de partición (NFR-LAZ-07)."""

RETENTION: Final = {
    **{
        table: "ledger_projection"
        for table in TABLES
        if table not in ("assistant_request", "assistant_draft", "loop_projection_checkpoint")
    },
    "assistant_request": "operational_state_with_retention",
    "assistant_draft": "operational_state_with_retention",
    "loop_projection_checkpoint": "operational_state",
}
"""Categoría de retención de cada tabla (NFR-LAZ-08, ``domain-entities.md`` §9): las
proyecciones 🔁 (con ``closure_attachment``, evidencia que no caduca), el asistente con 24 meses y
el control de la proyección sin retención."""

_ENTITIES: Final = {
    "episode": "Episode (domain-entities §2.1)",
    "finding_state": "FindingState (domain-entities §2.2)",
    "classification": "Classification (domain-entities §2.3)",
    "review_queue_item": "ReviewQueueItem (domain-entities §2.4)",
    "review_resolution": "ReviewResolution (domain-entities §2.4)",
    "corrective_action": "CorrectiveAction (domain-entities §2.5)",
    "action_evidence_entry": "ActionEvidenceEntry (domain-entities §2.6)",
    "closure_attachment": "ClosureEvidenceAttachment (domain-entities §2.6 y §6.3)",
    "closure_record": "ClosureRecord (domain-entities §2.6)",
    "unclosable_declaration": "UnclosableDeclaration (domain-entities §2.7)",
    "unclosable_review": "UnclosableReview (domain-entities §2.7)",
    "reopening": "Reopening (domain-entities §2.8)",
    "root_cause_analysis": "RootCauseAnalysis (domain-entities §2.9)",
    "assistant_request": "AssistantRequest (domain-entities §2.11)",
    "assistant_draft": "AssistantDraft (domain-entities §2.11)",
    "blocked_vocabulary_list": "BlockedVocabularyList (domain-entities §2.12)",
    "blocked_term": "BlockedTerm (domain-entities §2.12)",
    "plant_loop_configuration": "PlantLoopConfiguration (domain-entities §2.13)",
    "loop_projection_checkpoint": "LoopProjectionCheckpoint (domain-entities §2.14)",
}

APPEND_ONLY_TABLES: Final = ("closure_attachment", "assistant_request")
"""Tablas ⛓ (``migrations/append_only.py``)."""

CLOSING_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "closure_attachment": ("verified_at",),
    "assistant_request": ("resolved_at", "unavailable_cause"),
}
"""Lista blanca de cierres: de nulo a su valor una sola vez."""

STATE_TRANSITIONS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "assistant_request": (
        "state",
        ("pending>ready", "pending>unavailable", "pending>rejected_by_validators"),
    ),
}
"""Columna de estado y transiciones admitidas (solo hacia adelante)."""

WORK_COLUMNS: Final[dict[str, tuple[str, ...]]] = {"assistant_request": ("attempt_count",)}
"""Columnas de trabajo de una tabla ⛓ que cambian sin ser cierre (cuarto argumento de
``catalog.guard_update``): el contador de intentos de la solicitud."""

_FINDING_STATE_FIXED: Final = (
    "finding_state_id",
    "organization_id",
    "plant_id",
    "zone_id",
    "subject_kind",
    "subject_id",
    "anchor_record_id",
    "family",
    "standard_id",
    "standard_version",
    "occurred_at_exact",
    "occurred_at_bucket",
)
"""Lo que nace con el hallazgo y no cambia nunca: la versión del estándar con que se evaluó
(H-16) y el instante del hecho, que además es la columna de partición."""

_MUTABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "episode": (
        "tier",
        "segment_record_ids",
        "segment_count",
        "expected_segment_count",
        "missing_segment_indexes",
        "started_at",
        "ended_at",
        "total_episode_duration_ms",
        "total_condition_duration_ms",
        "max_confidence",
        "clip_references",
        "continues_open",
        "last_segment_received_at",
        "last_event_id",
        "projection_updated_at",
    ),
    "finding_state": (
        "tier",
        "lifecycle_state",
        "previous_lifecycle_state",
        "current_classification_outcome",
        "current_reason_category",
        "classification_count",
        "first_classified_at",
        "last_classified_at",
        "classified_within_deadline",
        "current_action_id",
        "current_action_type",
        "assignee_user_id",
        "assignee_inactive",
        "due_at",
        "overdue_since",
        "overdue_days",
        "closed_at",
        "closure_record_id",
        "unclosable_since",
        "unclosable_review_due_at",
        "unclosable_pending_review",
        "closed_with_later_different_classification",
        "reopening_count",
        "has_signed_root_cause",
        "observability_state_at_event",
        "zone_gate_status_at_event",
        "zone_agreement_revoked",
        "zone_agreement_revoked_at",
        "single_occupancy_zone",
        "aggregation_window_minutes",
        "walk_test_regression_pending",
        "walk_test_regression_cause",
        "age_days",
        "evidence_flagged",
        "missing_segments_declared",
        "last_event_id",
        "last_record_id",
        "projection_updated_at",
        "projection_digest",
    ),
    "review_queue_item": ("state", "resolved_at", "reminder_sent_at", "reminders_sent"),
    "corrective_action": ("state", "root_cause_analysis_id", "counts_as_overdue"),
    "unclosable_declaration": ("state", "review_due_at"),
    "root_cause_analysis": (
        "record_id",
        "state",
        "zone_context",
        "why_steps",
        "cause_effect_branches",
        "root_cause_category",
        "root_cause_statement",
        "conclusion",
        "missing_information",
        "assistant_used",
        "assistant_model",
        "assistant_version",
        "assistant_text_edited",
        "signer",
        "signed_at",
        "updated_at",
    ),
    "assistant_draft": ("accepted_by_human", "edited_by_human"),
    "blocked_vocabulary_list": (
        "version",
        "effective_from",
        "created_by",
        "record_id",
        "lemmatizer_version",
    ),
    "blocked_term": ("removed_in_version",),
    "plant_loop_configuration": (
        "classification_deadline_business_days",
        "review_deadline_business_days",
        "action_deadline_default_days",
        "action_deadline_min_days",
        "action_deadline_max_days",
        "manager_highlight_overdue_days",
        "unclosable_review_default_months",
        "unclosable_review_max_months",
        "monthly_package_generation_day",
        "sample_percent",
        "sample_min",
        "sample_max",
        "adoption_silence_days",
        "exposure_coverage_threshold_percent",
        "email_grouping_hours",
        "export_max_days",
        "export_max_zones",
        "single_occupancy_default_window_minutes",
        "reminder_interval_hours",
        "business_calendar",
        "timezone",
        "version",
        "changed_at",
        "changed_by",
        "change_reason",
    ),
    "loop_projection_checkpoint": (
        "last_applied_ledger_sequence",
        "last_applied_event_id",
        "projection_digest",
        "rebuilt_at",
        "last_verified_at",
        "divergence_count",
        "last_divergence_at",
        "state",
    ),
}
"""Proyecciones y estado operativo: columnas que el diseño deja cambiar. Las tablas de decisión
(``classification``, ``review_resolution``, ``action_evidence_entry``, ``closure_record``,
``unclosable_review``, ``reopening``) no tienen ninguna."""


def app_updatable_columns(table: str) -> tuple[str, ...]:
    """Columnas con ``UPDATE`` de ``vigia_app``: las mutables o los cierres de la tabla ⛓."""
    if table in _MUTABLE_COLUMNS:
        return _MUTABLE_COLUMNS[table]
    state = STATE_TRANSITIONS.get(table)
    return (
        CLOSING_COLUMNS.get(table, ())
        + ((state[0],) if state else ())
        + WORK_COLUMNS.get(table, ())
    )


def _array_literal(items: tuple[str, ...]) -> str:
    return "{" + ",".join(items) + "}"


def _update_guard(table: str) -> str:
    state, transitions = STATE_TRANSITIONS.get(table, ("", ()))
    arguments = (
        _array_literal(CLOSING_COLUMNS.get(table, ())),
        state,
        _array_literal(transitions),
        _array_literal(WORK_COLUMNS.get(table, ())),
    )
    return "catalog.guard_update(" + ", ".join(f"'{argument}'" for argument in arguments) + ")"


def _row_security() -> list[str]:
    statements: list[str] = []
    for table in TABLES:
        statements += row_security(
            SCHEMA,
            table,
            plant_column="plant_id" if table in PLANT_TABLES else None,
            organization_only=table in ORGANIZATION_ONLY_TABLES,
        )
    return statements


def _triggers() -> list[str]:
    statements: list[str] = []
    for table in APPEND_ONLY_TABLES:
        statements += [
            f"CREATE TRIGGER append_only_update BEFORE UPDATE ON loop.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {_update_guard(table)}",
            f"CREATE TRIGGER append_only_delete BEFORE DELETE ON loop.{table}"
            " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
            f"CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON loop.{table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()",
        ]
        for trigger in ("append_only_update", "append_only_delete", "append_only_no_truncate"):
            statements.append(f"ALTER TABLE loop.{table} ENABLE ALWAYS TRIGGER {trigger}")
    return statements


# Las particiones, después de crear los índices de la tabla padre: nacen con los suyos.
_MONTH_PARTITIONS = f"""
DO $$
DECLARE
    first_month CONSTANT timestamp := date_trunc('month', now() AT TIME ZONE 'UTC');
    month_start timestamp;
    parent text;
BEGIN
    FOREACH parent IN ARRAY ARRAY[{_in(tuple(PARTITIONED_TABLES))}] LOOP
        EXECUTE format('CREATE TABLE loop.%I PARTITION OF loop.%I DEFAULT',
                       parent || '_default', parent);
        FOR offset_months IN 0..{PARTITION_MONTHS_AHEAD} LOOP
            month_start := first_month + make_interval(months => offset_months);
            EXECUTE format(
                'CREATE TABLE loop.%I PARTITION OF loop.%I FOR VALUES FROM (%L) TO (%L)',
                parent || to_char(month_start, '"_"YYYY"_"MM'),
                parent,
                to_char(month_start, 'YYYY-MM-DD HH24:MI:SS') || '+00',
                to_char(month_start + interval '1 month', 'YYYY-MM-DD HH24:MI:SS') || '+00'
            );
        END LOOP;
    END LOOP;
END
$$
"""

_PARTITION_FUNCTIONS = (
    # Las funciones de nuc_0016 y gob_0018 no se tocan: la imagen N-1 convierte cada fila que
    # devuelven a su PartitionedTable y una fila de loop haría fallar (y revertir) su
    # create_partitions (NFR-NUC-14). Mismo cuerpo, guarda de actor y candado que las de fleet,
    # sin proteger la partición como de solo anexar (la proyección se reconstruye).
    """
    CREATE FUNCTION shared.vigia_create_loop_month_partitions(first_month date, last_month date)
        RETURNS TABLE (parent text, partition text, month date, created boolean, blocked boolean)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET lock_timeout = '5s'
    AS $$
    #variable_conflict use_variable
    DECLARE
        target record;
        current_month date;
        range_start timestamptz;
        range_end timestamptz;
        partition_name text;
        has_rows boolean;
    BEGIN
        IF coalesce(pg_catalog.current_setting('vigia.actor_kind', true), '')
            NOT IN ('system', 'operator')
        THEN
            RAISE EXCEPTION 'solo el sistema o un operador crea particiones'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF first_month IS NULL OR last_month IS NULL
            OR first_month <> date_trunc('month', first_month)::date
            OR last_month <> date_trunc('month', last_month)::date
            OR last_month < first_month
            OR last_month > (first_month + interval '119 months')::date
        THEN
            RAISE EXCEPTION 'intervalo de meses no válido'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        -- El mismo candado que vigia_create_month_partitions: las tres se serializan entre sí.
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('vigia_create_month_partitions', 0));
        FOR target IN
            SELECT * FROM (VALUES
                ('loop', 'finding_state', 'occurred_at_bucket'),
                ('loop', 'classification', 'classified_at'),
                ('loop', 'review_resolution', 'resolved_at')
            ) AS tables (schema_name, table_name, key_column)
        LOOP
            current_month := first_month;
            WHILE current_month <= last_month LOOP
                range_start := (current_month::timestamp AT TIME ZONE 'UTC');
                range_end := ((current_month + interval '1 month')::timestamp AT TIME ZONE 'UTC');
                partition_name := target.table_name || to_char(current_month, '"_"YYYY"_"MM');
                parent := target.schema_name || '.' || target.table_name;
                partition := target.schema_name || '.' || partition_name;
                month := current_month;
                created := false;
                blocked := false;
                IF to_regclass(format('%I.%I', target.schema_name, partition_name)) IS NULL THEN
                    EXECUTE format(
                        'SELECT EXISTS (SELECT 1 FROM %I.%I WHERE %I >= $1 AND %I < $2)',
                        target.schema_name, target.table_name || '_default',
                        target.key_column, target.key_column)
                        INTO has_rows USING range_start, range_end;
                    IF has_rows THEN
                        blocked := true;
                    ELSE
                        BEGIN
                            EXECUTE format(
                                'CREATE TABLE %I.%I PARTITION OF %I.%I'
                                ' FOR VALUES FROM (%L) TO (%L)',
                                target.schema_name, partition_name,
                                target.schema_name, target.table_name,
                                range_start, range_end);
                            created := true;
                        EXCEPTION WHEN duplicate_table THEN
                            created := false;
                        END;
                    END IF;
                END IF;
                RETURN NEXT;
                current_month := (current_month + interval '1 month')::date;
            END LOOP;
        END LOOP;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_create_loop_month_partitions(date, date) IS
        'Particiones mensuales que falten de las tres tablas particionadas de loop; salta el mes '
        'con filas en la partición por defecto (TASK-301)'
    """,
    """
    CREATE FUNCTION shared.vigia_loop_default_partition_rows()
        RETURNS TABLE (parent text, row_count bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF coalesce(pg_catalog.current_setting('vigia.actor_kind', true), '')
            NOT IN ('system', 'operator')
        THEN
            RAISE EXCEPTION 'solo el sistema o un operador lee la partición por defecto'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN QUERY
            SELECT 'loop.finding_state', count(*) FROM loop.finding_state_default
            UNION ALL
            SELECT 'loop.classification', count(*) FROM loop.classification_default
            UNION ALL
            SELECT 'loop.review_resolution', count(*) FROM loop.review_resolution_default;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_loop_default_partition_rows() IS
        'Filas de cada partición por defecto de loop (default_partition_rows, NFR-LAZ-07)'
    """,
)


def _grants() -> list[str]:
    statements = ["GRANT USAGE ON SCHEMA loop TO vigia_app"]
    for signature in (
        "shared.vigia_create_loop_month_partitions(date, date)",
        "shared.vigia_loop_default_partition_rows()",
    ):
        statements += [
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
            f"GRANT EXECUTE ON FUNCTION {signature} TO vigia_app",
        ]
    for table in TABLES:
        statements.append(f"GRANT SELECT, INSERT ON loop.{table} TO vigia_app")
        columns = app_updatable_columns(table)
        if columns:
            statements.append(f"GRANT UPDATE ({', '.join(columns)}) ON loop.{table} TO vigia_app")
    return statements


def _comments() -> list[str]:
    statements = [
        "COMMENT ON SCHEMA loop IS 'Módulo loop de U-04: proyección del lazo, decisiones,"
        " acciones, asistente, vocabulario y configuración'",
        "COMMENT ON INDEX loop.finding_state_inbox IS"
        " 'Bandeja: los siete estados activos, en orden de cubo (PAT-LAZ-ESC-03)'",
        "COMMENT ON INDEX loop.finding_state_overdue IS"
        " 'Panel de vencidos: action_assigned y overdue por plazo (PAT-LAZ-ESC-03)'",
        "COMMENT ON INDEX loop.assistant_request_rate IS"
        " 'Límite exacto del asistente por usuario y ventana (PAT-LAZ-ESC-02)'",
    ]
    for table in TABLES:
        mark = " ⛓ solo anexar;" if table in APPEND_ONLY_TABLES else ""
        provider = (
            "sin filas bajo concesión de proveedor (A-69)"
            if table in ORGANIZATION_ONLY_TABLES
            else "concesión de proveedor por planta"
        )
        statements.append(
            f"COMMENT ON TABLE loop.{table} IS '{_ENTITIES[table]};{mark}"
            f" seguridad a nivel de fila forzada por organización y {provider};"
            f" {retention_tag(RETENTION[table])}'"
        )
    return statements


def upgrade() -> None:
    # Como en gob_0017: todo lo creado es de vigia_migrate, lo ejecute el maestro o él.
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute("CREATE SCHEMA loop AUTHORIZATION vigia_migrate")
    for statement in (
        *_TABLES,
        _MONTH_PARTITIONS,
        *_PARTITION_FUNCTIONS,
        *_row_security(),
        *_triggers(),
        *_grants(),
        *_comments(),
    ):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
