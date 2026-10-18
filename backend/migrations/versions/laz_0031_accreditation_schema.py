"""Esquema ``accreditation``: exportación, paquete mensual, muestra de auditoría, compromisos,
métricas, exposición y adopción (TASK-338, LC-LAZ-02; NFR-LAZ-07 y 08; PAT-LAZ-RES-01,
PAT-LAZ-ESC-02 y 04, PAT-LAZ-SEG-03; U04-H-02, 03 y 08).

Revisión laz_0031, tras ``laz_0030`` (``notifications``). Las nueve entidades de
``domain-entities.md`` §3.1 a §3.6 (``ExportRequest`` y ``ExportPackage``, ``MonthlyPackage``,
``AuditSample``, ``AuditSampleItem``, ``Commitment``, ``ProgramMetricSnapshot``,
``ExposureIndicator`` y ``AdoptionCounter``) con sus notas fechadas. Los servicios (TASK-315 a
323 y 345 a 347) solo escriben repositorios encima.

- **``export_request``** es la única tabla con trabajo largo (PAT-LAZ-RES-01): ``state`` es
  ``export_state`` de §1 (sin ``composing``, U04-H-02), ``package_form`` (U04-H-03), arrendamiento
  (``lease_owner`` y ``lease_until``, los dos o ninguno), progreso (``progress_percent``,
  ``progress_updated_at``, ``current_artifact`` ≤ 128), subida por partes
  (``multipart_upload_id`` ≤ 256) y ``attempt_count``. **Reproducibilidad** (U04-H-08):
  ``checkpoint_record_ids`` y ``generated_at`` nacen nulos, se fijan **juntos** en el primer
  intento y el disparador ``export_request_fixed_on_first_attempt`` impide cambiarlos después;
  una retoma los reutiliza. ``zone_ids`` lleva de 1 a 20 zonas y el periodo, a lo sumo 366 días;
  que las zonas sean de la planta lo valida el servicio (TASK-315).
- **Límite exacto** (PAT-LAZ-ESC-02): ``export_request_rate`` por
  ``(organization_id, user_id, requested_at)``. **Idempotencia por zona y periodo**
  (PAT-LAZ-RES-05): ``UNIQUE (organization_id, zone_id, period)`` en ``monthly_package``,
  ``program_metric_snapshot`` y ``exposure_indicator``; ``period`` es el primer día del mes.
- **``audit_sample_item`` ⛓ solo anexar** (``catalog.guard_update()`` de gob_0017, con
  ``ENABLE ALWAYS``): el resultado de la revisión (``outcome``, ``comment``, ``reviewed_by``,
  ``reviewed_at``, ``record_id``) pasa una sola vez de nulo a valor; nada más cambia (H-39).
  ``DELETE`` y ``TRUNCATE`` fallan para cualquier rol.
- **``adoption_counter`` sin usuario** (H-33, P3): ninguna columna referencia a un usuario y
  ``activity_counts`` solo admite claves de ``loop_activity_kind`` con enteros ≥ 0.
- Toda tabla lleva ``organization_id`` y ``plant_id`` (y ``zone_id`` salvo las dos de
  exportación), con claves foráneas a ``identity.plant`` o ``identity.zone``; los usuarios, a
  ``identity.user_account (organization_id, user_id)``. Las tablas hijas apuntan a su padre por
  ``(organization_id, plant_id[, zone_id], id)``: un paquete, una muestra o un compromiso nunca
  cambian de planta ni de zona respecto a lo que cuelgan.
- **Seguridad a nivel de fila** (``migrations/laz_policies.py``): ``organization_isolation``
  forzada y ``provider_concession_scope`` RESTRICTIVE en las nueve (todas tienen planta; A-69 no
  añade nada aquí).
- ``vigia_app``: ``SELECT`` e ``INSERT`` en todas; ``UPDATE`` solo en las columnas que el diseño
  deja cambiar (``app_updatable_columns``): nunca claves, organización, planta ni zona;
  ``export_package`` no tiene ninguna. Nunca ``DELETE`` ni ``TRUNCATE``.
- **Retención** (NFR-LAZ-08, DE §9): ``COMMENT ON TABLE`` declara la categoría (``RETENTION``) y
  el plazo (``RETENTION_PERIODS``): ``export_request`` es estado operativo con 24 meses (el
  paquete producido no se borra: su objeto vive en el depósito), ``adoption_counter`` estado
  operativo sin caducidad y el resto, proyección del expediente. La tarea de purga no existe en v1.

Lo que no hace: los servicios, las rutas, el registro de tipos ni subir
``MINIMUM_SCHEMA_VERSION``.
"""

from __future__ import annotations

from typing import Final

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0031"
down_revision: str | None = "laz_0030"
branch_labels: None = None
depends_on: None = None

SCHEMA: Final = "accreditation"

TABLES: Final = (
    "export_request",
    "export_package",
    "monthly_package",
    "audit_sample",
    "audit_sample_item",
    "commitment",
    "program_metric_snapshot",
    "exposure_indicator",
    "adoption_counter",
)
"""Las nueve tablas de DE §3.1 a §3.6, todas con planta (``provider_concession_scope``)."""

PERIOD_KEY_TABLES: Final = ("monthly_package", "program_metric_snapshot", "exposure_indicator")
"""Las de clave de idempotencia ``(organization_id, zone_id, period)`` (PAT-LAZ-RES-05)."""

APPEND_ONLY_TABLES: Final = ("audit_sample_item",)
"""Tablas ⛓ (``catalog.guard_update``)."""

CLOSING_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "audit_sample_item": ("outcome", "comment", "reviewed_by", "reviewed_at", "record_id"),
}
"""Lista blanca de cierres: de nulo a su valor una sola vez."""

FIXED_ON_FIRST_ATTEMPT: Final = ("checkpoint_record_ids", "generated_at")
"""Columnas de ``export_request`` que se fijan en el primer intento y no cambian (U04-H-08)."""

RETENTION: Final = {
    **{table: "ledger_projection" for table in TABLES},
    "export_request": "operational_state_with_retention",
    "adoption_counter": "operational_state",
}
"""Categoría de cada tabla (DE §9): ``ExportRequest`` y ``AdoptionCounter`` no son proyección."""

RETENTION_PERIODS: Final = {
    **{table: "sin caducidad (proyección del expediente)" for table in TABLES},
    "export_request": "24 meses (el paquete producido no se borra: su objeto vive en el depósito)",
    "adoption_counter": "sin caducidad",
}

_ENTITIES: Final = {
    "export_request": "ExportRequest (domain-entities §3.1)",
    "export_package": "ExportPackage (domain-entities §3.1)",
    "monthly_package": "MonthlyPackage (domain-entities §3.3)",
    "audit_sample": "AuditSample (domain-entities §3.3)",
    "audit_sample_item": "AuditSampleItem (domain-entities §3.3)",
    "commitment": "Commitment (domain-entities §3.3)",
    "program_metric_snapshot": "ProgramMetricSnapshot (domain-entities §3.4)",
    "exposure_indicator": "ExposureIndicator (domain-entities §3.5)",
    "adoption_counter": "AdoptionCounter (domain-entities §3.6)",
}

_EXPORT_STATE = "'requested', 'in_progress', 'ready', 'failed', 'expired'"
"""``export_state`` (§1 y U04-H-02: no existe ``composing``)."""
_PACKAGE_FORM = "'with_clips', 'without_clips'"
"""``package_form`` (U04-H-03)."""
_MONTHLY_PACKAGE_STATE = "'generating', 'sealed', 'failed'"
_SAMPLE_AUDIT_OUTCOME = "'consistent_criterion', 'doubtful_criterion', 'criterion_error'"
_COMMITMENT_STATE = "'open', 'done', 'dropped'"
_EXPOSURE_STATUS = "'computed', 'partial', 'not_computable'"
_LOOP_ACTIVITY_KIND = (
    "'inbox_opened', 'finding_classified', 'queue_resolved', 'analysis_advanced',"
    " 'action_managed', 'finding_closed'"
)
"""``loop_activity_kind`` (§1): las únicas claves de ``activity_counts``."""
_CODE = "'^[a-z][a-z0-9_]*$'"
_HEX64 = "'^[0-9a-f]{64}$'"


def _length(column: str, low: int, high: int) -> str:
    return f"CHECK (char_length({column}) BETWEEN {low} AND {high})"


def _code(column: str, high: int) -> str:
    return f"CHECK (char_length({column}) <= {high} AND {column} ~ {_CODE})"


def _hex64(column: str) -> str:
    return f"CHECK ({column} ~ {_HEX64})"


def _ids(column: str) -> str:
    """Lista de una dimensión sin nulos (nula si la columna lo admite)."""
    return (
        f"CHECK (CASE WHEN COALESCE(array_ndims({column}), 1) = 1"
        f" THEN array_position({column}, NULL) IS NULL ELSE false END)"
    )


def _json(column: str, kind: str) -> str:
    return f"CHECK (jsonb_typeof({column}) = '{kind}')"


def _month(column: str) -> str:
    """El periodo es un mes calendario: se guarda su primer día (en la zona de la planta)."""
    return f"CHECK (extract(day FROM {column}) = 1)"


_ZONE_FKEY = (
    "FOREIGN KEY (organization_id, plant_id, zone_id)"
    " REFERENCES identity.zone (organization_id, plant_id, zone_id)"
)


def _parent_fkey(table: str, column: str) -> str:
    """El hijo cuelga de su padre en la misma organización, planta y zona."""
    columns = f"organization_id, plant_id, zone_id, {column}"
    return f"FOREIGN KEY ({columns}) REFERENCES accreditation.{table} ({columns})"


def _user_fkey(column: str) -> str:
    return (
        f"FOREIGN KEY (organization_id, {column})"
        " REFERENCES identity.user_account (organization_id, user_id)"
    )


_TABLES = (
    # §3.1 ExportRequest 🔒 con las notas de 2026-09-21 (PAT-LAZ-RES-01) y U04-H-02, 03 y 08.
    f"""
    CREATE TABLE accreditation.export_request (
        export_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_ids uuid[] NOT NULL
            CONSTRAINT export_request_zone_ids {_ids("zone_ids")}
            CONSTRAINT export_request_zone_count CHECK (cardinality(zone_ids) BETWEEN 1 AND 20),
        period_from timestamptz NOT NULL,
        period_to timestamptz NOT NULL,
        requested_by jsonb NOT NULL
            CONSTRAINT export_request_requested_by {_json("requested_by", "object")},
        user_id uuid NOT NULL,
        requested_at timestamptz NOT NULL,
        state text NOT NULL DEFAULT 'requested'
            CONSTRAINT export_request_state_values CHECK (state IN ({_EXPORT_STATE})),
        package_form text NOT NULL
            CONSTRAINT export_request_package_form CHECK (package_form IN ({_PACKAGE_FORM})),
        progress_percent integer NOT NULL DEFAULT 0
            CONSTRAINT export_request_progress CHECK (progress_percent BETWEEN 0 AND 100),
        progress_updated_at timestamptz,
        current_artifact text
            CONSTRAINT export_request_current_artifact {_length("current_artifact", 1, 128)},
        failure_code text CONSTRAINT export_request_failure_code {_code("failure_code", 64)},
        started_at timestamptz,
        completed_at timestamptz,
        expires_at timestamptz NOT NULL,
        record_id uuid,
        correlation_id uuid NOT NULL,
        lease_owner text CONSTRAINT export_request_lease_owner {_length("lease_owner", 1, 128)},
        lease_until timestamptz,
        multipart_upload_id text
            CONSTRAINT export_request_multipart {_length("multipart_upload_id", 1, 256)},
        attempt_count integer NOT NULL DEFAULT 0
            CONSTRAINT export_request_attempt_count CHECK (attempt_count >= 0),
        checkpoint_record_ids uuid[]
            CONSTRAINT export_request_checkpoints {_ids("checkpoint_record_ids")},
        generated_at timestamptz,
        CONSTRAINT export_request_pkey PRIMARY KEY (export_id),
        CONSTRAINT export_request_scope UNIQUE (organization_id, plant_id, export_id),
        CONSTRAINT export_request_period CHECK (
            period_to > period_from AND period_to - period_from <= interval '366 days'
        ),
        CONSTRAINT export_request_lease CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
        CONSTRAINT export_request_first_attempt
            CHECK ((checkpoint_record_ids IS NULL) = (generated_at IS NULL)),
        CONSTRAINT export_request_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT export_request_user_fkey {_user_fkey("user_id")}
    )
    """,
    "CREATE INDEX export_request_rate"
    " ON accreditation.export_request (organization_id, user_id, requested_at)",
    "CREATE INDEX export_request_pending ON accreditation.export_request"
    " (organization_id, requested_at) WHERE state IN ('requested', 'in_progress')",
    # §3.1 ExportPackage 🔒🔁: el resultado de una composición completa; no cambia.
    f"""
    CREATE TABLE accreditation.export_package (
        export_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        document_sha256 text NOT NULL
            CONSTRAINT export_package_document_sha256 {_hex64("document_sha256")},
        archive_sha256 text NOT NULL
            CONSTRAINT export_package_archive_sha256 {_hex64("archive_sha256")},
        manifest jsonb NOT NULL CONSTRAINT export_package_manifest {_json("manifest", "object")},
        size_bytes bigint NOT NULL CONSTRAINT export_package_size CHECK (size_bytes >= 0),
        artifact_count integer NOT NULL
            CONSTRAINT export_package_artifact_count CHECK (artifact_count >= 0),
        checkpoint_record_ids uuid[] NOT NULL
            CONSTRAINT export_package_checkpoints {_ids("checkpoint_record_ids")},
        verifier_version text NOT NULL
            CONSTRAINT export_package_verifier_version {_length("verifier_version", 1, 64)},
        CONSTRAINT export_package_pkey PRIMARY KEY (export_id),
        CONSTRAINT export_package_request_fkey FOREIGN KEY (organization_id, plant_id, export_id)
            REFERENCES accreditation.export_request (organization_id, plant_id, export_id)
    )
    """,
    # §3.3 MonthlyPackage 🔒🔁: uno por zona y mes.
    f"""
    CREATE TABLE accreditation.monthly_package (
        package_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        period date NOT NULL CONSTRAINT monthly_package_month {_month("period")},
        state text NOT NULL
            CONSTRAINT monthly_package_state_values CHECK (state IN ({_MONTHLY_PACKAGE_STATE})),
        generated_at timestamptz NOT NULL,
        findings_summary jsonb NOT NULL
            CONSTRAINT monthly_package_findings {_json("findings_summary", "object")},
        recurrence_summary jsonb NOT NULL
            CONSTRAINT monthly_package_recurrence {_json("recurrence_summary", "array")},
        metric_snapshot_id uuid,
        exposure_indicator_id uuid,
        coverage_summary jsonb NOT NULL
            CONSTRAINT monthly_package_coverage {_json("coverage_summary", "object")},
        commitments_from_previous_period jsonb NOT NULL
            CONSTRAINT monthly_package_previous_commitments
                {_json("commitments_from_previous_period", "array")},
        audit_sample_id uuid,
        sealed_checkpoint_record_id uuid,
        record_id uuid,
        CONSTRAINT monthly_package_pkey PRIMARY KEY (package_id),
        CONSTRAINT monthly_package_period UNIQUE (organization_id, zone_id, period),
        CONSTRAINT monthly_package_scope UNIQUE (organization_id, plant_id, zone_id, package_id),
        CONSTRAINT monthly_package_zone_fkey {_ZONE_FKEY}
    )
    """,
    # §3.3 AuditSample 🔒🔁: la semilla queda registrada para reproducir la selección.
    f"""
    CREATE TABLE accreditation.audit_sample (
        sample_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        package_id uuid NOT NULL,
        population_size integer NOT NULL
            CONSTRAINT audit_sample_population CHECK (population_size >= 0),
        sample_size integer NOT NULL
            CONSTRAINT audit_sample_size CHECK (
                sample_size BETWEEN 0 AND 30 AND sample_size <= population_size
            ),
        seed bigint NOT NULL,
        strata jsonb NOT NULL CONSTRAINT audit_sample_strata {_json("strata", "array")},
        selected_at timestamptz NOT NULL,
        reviewer_user_id uuid NOT NULL,
        review_completed_at timestamptz,
        CONSTRAINT audit_sample_pkey PRIMARY KEY (sample_id),
        CONSTRAINT audit_sample_scope UNIQUE (organization_id, plant_id, zone_id, sample_id),
        CONSTRAINT audit_sample_package_fkey {_parent_fkey("monthly_package", "package_id")},
        CONSTRAINT audit_sample_reviewer_fkey {_user_fkey("reviewer_user_id")}
    )
    """,
    "CREATE INDEX audit_sample_package ON accreditation.audit_sample (organization_id, package_id)",
    # §3.3 AuditSampleItem 🔒🔁 ⛓: observa y deja constancia; no cambia ninguna clasificación.
    f"""
    CREATE TABLE accreditation.audit_sample_item (
        sample_item_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        sample_id uuid NOT NULL,
        classification_id uuid NOT NULL,
        subject_id uuid NOT NULL,
        outcome text
            CONSTRAINT audit_sample_item_outcome CHECK (outcome IN ({_SAMPLE_AUDIT_OUTCOME})),
        comment text CONSTRAINT audit_sample_item_comment {_length("comment", 0, 1000)},
        reviewed_by jsonb
            CONSTRAINT audit_sample_item_reviewed_by {_json("reviewed_by", "object")},
        reviewed_at timestamptz,
        record_id uuid,
        CONSTRAINT audit_sample_item_pkey PRIMARY KEY (sample_item_id),
        CONSTRAINT audit_sample_item_review CHECK (
            (outcome IS NULL) = (reviewed_at IS NULL) AND (outcome IS NULL) = (reviewed_by IS NULL)
        ),
        CONSTRAINT audit_sample_item_sample_fkey {_parent_fkey("audit_sample", "sample_id")}
    )
    """,
    "CREATE INDEX audit_sample_item_sample"
    " ON accreditation.audit_sample_item (organization_id, sample_id)",
    # §3.3 Commitment 🔒🔁: lo único que registra el mando de línea.
    f"""
    CREATE TABLE accreditation.commitment (
        commitment_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        package_id uuid NOT NULL,
        text text NOT NULL CONSTRAINT commitment_text {_length("text", 20, 1000)},
        responsible_user_id uuid NOT NULL,
        due_date date NOT NULL,
        state text NOT NULL
            CONSTRAINT commitment_state_values CHECK (state IN ({_COMMITMENT_STATE})),
        recorded_by jsonb NOT NULL
            CONSTRAINT commitment_recorded_by {_json("recorded_by", "object")},
        recorded_at timestamptz NOT NULL,
        record_id uuid NOT NULL,
        closed_note text CONSTRAINT commitment_closed_note {_length("closed_note", 0, 500)},
        closed_at timestamptz,
        CONSTRAINT commitment_pkey PRIMARY KEY (commitment_id),
        CONSTRAINT commitment_package_fkey {_parent_fkey("monthly_package", "package_id")},
        CONSTRAINT commitment_responsible_fkey {_user_fkey("responsible_user_id")}
    )
    """,
    "CREATE INDEX commitment_package ON accreditation.commitment (organization_id, package_id)",
    # §3.4 ProgramMetricSnapshot 🔒🔁: ninguna columna desagrega por persona, equipo ni turno.
    f"""
    CREATE TABLE accreditation.program_metric_snapshot (
        snapshot_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        period date NOT NULL CONSTRAINT program_metric_snapshot_month {_month("period")},
        granularity text NOT NULL
            CONSTRAINT program_metric_snapshot_granularity CHECK (granularity = 'month'),
        values jsonb NOT NULL CONSTRAINT program_metric_snapshot_values {_json("values", "array")},
        materialized_at timestamptz NOT NULL,
        recomputed_at timestamptz,
        inputs_digest text NOT NULL
            CONSTRAINT program_metric_snapshot_inputs_digest {_hex64("inputs_digest")},
        CONSTRAINT program_metric_snapshot_pkey PRIMARY KEY (snapshot_id),
        CONSTRAINT program_metric_snapshot_period UNIQUE (organization_id, zone_id, period),
        CONSTRAINT program_metric_snapshot_zone_fkey {_ZONE_FKEY}
    )
    """,
    # §3.5 ExposureIndicator 🔒🔁 con los tres campos de la nota de 2026-09-21.
    f"""
    CREATE TABLE accreditation.exposure_indicator (
        exposure_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        period date NOT NULL CONSTRAINT exposure_indicator_month {_month("period")},
        status text NOT NULL
            CONSTRAINT exposure_indicator_status_values CHECK (status IN ({_EXPOSURE_STATUS})),
        confirmed_minutes double precision NOT NULL
            CONSTRAINT exposure_indicator_confirmed CHECK (confirmed_minutes >= 0),
        pending_classification_minutes double precision NOT NULL
            CONSTRAINT exposure_indicator_pending CHECK (pending_classification_minutes >= 0),
        observable_coverage_percent double precision NOT NULL
            CONSTRAINT exposure_indicator_coverage
                CHECK (observable_coverage_percent BETWEEN 0 AND 100),
        coverage_timeline_ref jsonb NOT NULL
            CONSTRAINT exposure_indicator_timeline {_json("coverage_timeline_ref", "object")},
        hourly_distribution jsonb NOT NULL
            CONSTRAINT exposure_indicator_hourly {_json("hourly_distribution", "array")},
        not_computable_reason_es text
            CONSTRAINT exposure_indicator_not_computable
                {_length("not_computable_reason_es", 1, 1000)},
        partial_reason_es text
            CONSTRAINT exposure_indicator_partial {_length("partial_reason_es", 1, 1000)},
        usage_notice_es text NOT NULL
            CONSTRAINT exposure_indicator_usage_notice {_length("usage_notice_es", 1, 2000)},
        materialized_at timestamptz NOT NULL,
        recomputed_at timestamptz,
        inputs_digest text NOT NULL
            CONSTRAINT exposure_indicator_inputs_digest {_hex64("inputs_digest")},
        CONSTRAINT exposure_indicator_pkey PRIMARY KEY (exposure_id),
        CONSTRAINT exposure_indicator_period UNIQUE (organization_id, zone_id, period),
        CONSTRAINT exposure_indicator_zone_fkey {_ZONE_FKEY}
    )
    """,
    # §3.6 AdoptionCounter 🔒: actividad por zona y día; nunca qué usuario hizo qué (H-33, P3).
    f"""
    CREATE TABLE accreditation.adoption_counter (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        day date NOT NULL,
        activity_counts jsonb NOT NULL
            CONSTRAINT adoption_counter_activity_counts CHECK (
                jsonb_typeof(activity_counts) = 'object'
                AND activity_counts - ARRAY[{_LOOP_ACTIVITY_KIND}]::text[] = '{{}}'::jsonb
                AND NOT jsonb_path_exists(
                    activity_counts,
                    '$.* ? (@.type() != "number" || @ < 0 || @.floor() != @)'
                )
            ),
        session_count integer NOT NULL
            CONSTRAINT adoption_counter_session_count CHECK (session_count >= 0),
        last_activity_at timestamptz,
        CONSTRAINT adoption_counter_pkey PRIMARY KEY (organization_id, zone_id, day),
        CONSTRAINT adoption_counter_zone_fkey {_ZONE_FKEY}
    )
    """,
)

_MUTABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "export_request": (
        "state",
        "progress_percent",
        "progress_updated_at",
        "current_artifact",
        "failure_code",
        "started_at",
        "completed_at",
        "expires_at",
        "record_id",
        "lease_owner",
        "lease_until",
        "multipart_upload_id",
        "attempt_count",
        *FIXED_ON_FIRST_ATTEMPT,
    ),
    "monthly_package": (
        "state",
        "generated_at",
        "findings_summary",
        "recurrence_summary",
        "metric_snapshot_id",
        "exposure_indicator_id",
        "coverage_summary",
        "commitments_from_previous_period",
        "audit_sample_id",
        "sealed_checkpoint_record_id",
        "record_id",
    ),
    "audit_sample": ("review_completed_at",),
    "audit_sample_item": CLOSING_COLUMNS["audit_sample_item"],
    "commitment": ("state", "closed_note", "closed_at"),
    "program_metric_snapshot": ("values", "materialized_at", "recomputed_at", "inputs_digest"),
    "exposure_indicator": (
        "status",
        "confirmed_minutes",
        "pending_classification_minutes",
        "observable_coverage_percent",
        "coverage_timeline_ref",
        "hourly_distribution",
        "not_computable_reason_es",
        "partial_reason_es",
        "usage_notice_es",
        "materialized_at",
        "recomputed_at",
        "inputs_digest",
    ),
    "adoption_counter": ("activity_counts", "session_count", "last_activity_at"),
}
"""Columnas que el diseño deja cambiar. Nunca claves, organización, planta, zona ni el alcance de
la exportación; ``export_package`` es el resultado de una composición completa y no tiene
ninguna. ``FIXED_ON_FIRST_ATTEMPT`` se escribe una vez (disparador)."""


def app_updatable_columns(table: str) -> tuple[str, ...]:
    """Columnas con ``UPDATE`` de ``vigia_app``."""
    return _MUTABLE_COLUMNS.get(table, ())


def _row_security() -> list[str]:
    statements: list[str] = []
    for table in TABLES:
        statements += row_security(SCHEMA, table, plant_column="plant_id", organization_only=False)
    return statements


_FUNCTIONS = (
    # U04-H-08: los puntos de control y generated_at del primer intento se reutilizan al retomar.
    """
    CREATE FUNCTION accreditation.export_request_fixed_on_first_attempt() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF OLD.generated_at IS NOT NULL
           AND (NEW.generated_at IS DISTINCT FROM OLD.generated_at
                OR NEW.checkpoint_record_ids IS DISTINCT FROM OLD.checkpoint_record_ids) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'checkpoint_record_ids y generated_at se fijan en el primer intento'
                          ' y no cambian al retomar (U04-H-08)';
        END IF;
        RETURN NEW;
    END
    $$
    """,
)


def _array_literal(items: tuple[str, ...]) -> str:
    return "{" + ",".join(items) + "}"


def _triggers() -> list[str]:
    statements = [
        "CREATE TRIGGER export_request_fixed_on_first_attempt"
        " BEFORE UPDATE ON accreditation.export_request FOR EACH ROW"
        " EXECUTE FUNCTION accreditation.export_request_fixed_on_first_attempt()",
        "ALTER TABLE accreditation.export_request"
        " ENABLE ALWAYS TRIGGER export_request_fixed_on_first_attempt",
    ]
    for table in APPEND_ONLY_TABLES:
        guard = (
            f"catalog.guard_update('{_array_literal(CLOSING_COLUMNS[table])}', '', '{{}}', '{{}}')"
        )
        statements += [
            f"CREATE TRIGGER append_only_update BEFORE UPDATE ON accreditation.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {guard}",
            f"CREATE TRIGGER append_only_delete BEFORE DELETE ON accreditation.{table}"
            " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
            f"CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON accreditation.{table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()",
        ]
        for trigger in ("append_only_update", "append_only_delete", "append_only_no_truncate"):
            statements.append(f"ALTER TABLE accreditation.{table} ENABLE ALWAYS TRIGGER {trigger}")
    return statements


def _grants() -> list[str]:
    statements = ["GRANT USAGE ON SCHEMA accreditation TO vigia_app"]
    for table in TABLES:
        statements.append(f"GRANT SELECT, INSERT ON accreditation.{table} TO vigia_app")
        columns = app_updatable_columns(table)
        if columns:
            statements.append(
                f"GRANT UPDATE ({', '.join(columns)}) ON accreditation.{table} TO vigia_app"
            )
    return statements


def _comments() -> list[str]:
    statements = [
        "COMMENT ON SCHEMA accreditation IS 'Módulo accreditation de U-04: exportación, paquete"
        " mensual, muestra de auditoría, compromisos, métricas, exposición y adopción'",
        "COMMENT ON INDEX accreditation.export_request_rate IS"
        " 'Límite exacto de exportaciones por usuario y ventana (PAT-LAZ-ESC-02)'",
    ]
    for table in TABLES:
        mark = " ⛓ solo anexar;" if table in APPEND_ONLY_TABLES else ""
        statements.append(
            f"COMMENT ON TABLE accreditation.{table} IS '{_ENTITIES[table]};{mark}"
            " seguridad a nivel de fila forzada por organización y concesión de proveedor por"
            f" planta; retención: {RETENTION_PERIODS[table]}; {retention_tag(RETENTION[table])}'"
        )
    return statements


def upgrade() -> None:
    # Como en laz_0029: todo lo creado es de vigia_migrate, lo ejecute el maestro o él.
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute("CREATE SCHEMA accreditation AUTHORIZATION vigia_migrate")
    for statement in (
        *_TABLES,
        *_FUNCTIONS,
        *_triggers(),
        *_row_security(),
        *_grants(),
        *_comments(),
    ):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
