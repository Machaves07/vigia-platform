"""Esquema ``catalog``: gobernanza y comisionamiento (TASK-202, LC-GOB-21 parte 1, LC-GOB-03).

Revisión gob_0017, primer eslabón de U-03. Las 18 tablas de ``domain-entities.md`` §2 (más
``zone_camera`` y ``document_upload_grant``, notas de §3.14) con el aislamiento de U-02 y la
regla de solo anexar; las tareas de dominio de M2 y M3 solo escriben repositorios encima.

- ``btree_gist``: ya la crea ``nuc_0001`` (adenda A-32); el ``CREATE EXTENSION IF NOT EXISTS``
  es idempotente y deja escrita aquí la dependencia de R-GOB-14.
- Toda tabla lleva ``organization_id`` y ``plant_id``; las de zona referencian
  ``identity.zone (organization_id, plant_id, zone_id)`` y las de planta ``identity.plant``, así
  que la base garantiza que zona, planta y organización concuerdan (BR-NUC-08). Las marcas de
  tiempo no tienen valor por defecto: las pone la aplicación con su ``Clock``.
- ``gate_state_history`` (PAT-GOB-REN-01, PR-GOB-21): columna generada
  ``effective = tstzrange(effective_from, effective_until, '[)')`` y la restricción
  ``gate_state_no_overlap EXCLUDE USING gist (zone_id WITH =, gate WITH =, effective WITH &&)``:
  dos intervalos de la misma zona y compuerta no se solapan **en la base**. Un intervalo abierto
  (cota superior nula) es no acotado, así que el cierre del vigente y la apertura del siguiente
  van en la misma transacción (LC-GOB-03). ``effective_until > effective_from``: un rango vacío no
  chocaría con nada. La tabla no está particionada (R-GOB-14) y no caduca (NFR-GOB-16).
- ``FORCE ROW LEVEL SECURITY`` en las 18 con ``organization_isolation`` y la política RESTRICTIVE
  ``provider_concession_scope`` (``identity.rls_provider_scope_allows(organization_id,
  plant_id)``, adenda A-46 y ``nuc_0015``): todas tienen planta.
- Solo anexar (⛓, BR-NUC-43), con disparadores propios de ``catalog`` (los de LC-NUC-12 no se
  tocan): ``DELETE`` y ``TRUNCATE`` los rechaza ``shared.vigia_reject_mutation()`` para cualquier
  rol; el ``UPDATE`` lo custodia ``catalog.guard_update()`` con la **lista blanca** de la tabla
  (``CLOSING_COLUMNS``): ninguna otra columna cambia, un cierre pasa **una sola vez** de nulo a
  su valor y nunca vuelve a cambiar, y la columna de estado solo avanza por las transiciones de
  ``STATE_TRANSITIONS`` (``business-logic-model.md`` §3.2 y nota de §2.14). Las tablas ⛓ sin
  cierre rechazan todo ``UPDATE`` con ``shared.vigia_reject_mutation()``. Los disparadores se
  activan con ``ENABLE ALWAYS``: tampoco los apaga ``session_replication_role = replica``.
- ``document_upload_grant`` (🔒) solo cambia de estado, de ``issued`` a ``used`` o ``expired``,
  con el mismo disparador.
- ``vigia_app``: ``SELECT`` e ``INSERT`` en todas; ``UPDATE`` solo sobre las columnas de la lista
  blanca de las ⛓ y las que el diseño deja cambiar en las proyecciones 🔒; nunca ``DELETE`` ni
  ``TRUNCATE`` (P4: no existe borrado en ninguna entidad de §2).
- Restricciones que el diseño fija y la base puede comprobar: listas cerradas de §4 (con los
  valores añadidos por las notas fechadas) y las heredadas del contrato (``predicate_family``,
  ``gate_status``, ``zone_mode``, ``camera_role_in_zone``), longitudes de texto, ``resulting_mode``
  como función total de las dos compuertas (BR-CTR-40), ``valid_until = issued_at + 7 días``
  (BR-CTR-43), ``deadline = ended_at + 5 min`` (nota de §2.14), una familia admitida una vez por
  planta, una sesión de walk-test abierta por zona, una versión vigente por estándar y un
  estándar en una sola zona.

Lo que no hace: el esquema ``fleet`` (TASK-203), los repositorios y el dominio (TASK-207 a
TASK-216) ni subir ``MINIMUM_SCHEMA_VERSION`` (la primera tarea cuyo código use el esquema).
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0017"
down_revision: str | None = "nuc_0016"
branch_labels: None = None
depends_on: None = None

_ROLES = (
    "'coordinator_sst', 'line_manager', 'plant_manager', 'administrator', "
    "'provider_installer', 'copasst', 'platform_operator'"
)
"""``role`` de U-02 (§1): el rol en uso de quien firma, emite o decide."""

_PREDICATE_FAMILY = "'coexistence', 'guard_bypass', 'dwell', 'startup_transition'"
"""``predicate_family`` del contrato (U-01): la plataforma habilita familias, nunca las crea."""

_GATE_STATUS = "'pending', 'approved', 'revoked'"
"""``gate_status`` del contrato (U-01)."""

_CHANGED_FIELDS = (
    "ARRAY['standards', 'cameras', 'minimum_coverage', 'signals', 'thresholds',"
    " 'clip_window', 'episode', 'single_occupancy']::text[]"
)
"""``catalog_changed_field`` (§4)."""

_FUNCTIONS = (
    # Lista sin repetidos ni nulos, de una dimensión (changed_fields, required_roles).
    """
    CREATE FUNCTION catalog.text_array_is_set(items text[]) RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog
    AS $$
        SELECT pg_catalog.array_ndims(items) = 1
            AND pg_catalog.array_position(items, NULL) IS NULL
            AND (SELECT count(DISTINCT item) = count(*) FROM pg_catalog.unnest(items) AS item)
    $$
    """,
    # Guarda del UPDATE de una tabla de catalog. Argumentos del disparador:
    #   0: columnas de cierre ('{a,b}'): de nulo a su valor una sola vez, nunca más;
    #   1: columna de estado ('' si no hay);
    #   2: transiciones admitidas de la columna de estado ('{desde>hacia,...}');
    #   3: columnas generadas, que se derivan de las demás ('{}' si no hay).
    # Cualquier otra columna que cambie, un cierre que ya tenía valor y cambia, o un cambio de
    # estado fuera de la lista: restrict_violation (P4, BR-NUC-43).
    """
    CREATE FUNCTION catalog.guard_update() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    DECLARE
        closing CONSTANT text[] := TG_ARGV[0]::text[];
        state_column CONSTANT text := NULLIF(TG_ARGV[1], '');
        transitions CONSTANT text[] := TG_ARGV[2]::text[];
        whitelist text[] := closing || TG_ARGV[3]::text[];
        old_row CONSTANT jsonb := to_jsonb(OLD);
        new_row CONSTANT jsonb := to_jsonb(NEW);
        column_name text;
        reason text;
    BEGIN
        IF state_column IS NOT NULL THEN
            whitelist := whitelist || state_column;
        END IF;
        IF new_row - whitelist IS DISTINCT FROM old_row - whitelist THEN
            reason := 'solo cambian ' || array_to_string(closing || state_column, ', ');
        END IF;
        FOREACH column_name IN ARRAY closing LOOP
            IF reason IS NULL
               AND old_row -> column_name <> 'null'::jsonb
               AND new_row -> column_name IS DISTINCT FROM old_row -> column_name THEN
                reason := format('el cierre %s ya tenía valor y no vuelve a cambiar', column_name);
            END IF;
        END LOOP;
        IF reason IS NULL
           AND state_column IS NOT NULL
           AND new_row ->> state_column IS DISTINCT FROM old_row ->> state_column
           AND NOT (old_row ->> state_column) || '>' || (new_row ->> state_column)
                   = ANY (transitions) THEN
            reason := format('%s no pasa de %s a %s', state_column,
                             old_row ->> state_column, new_row ->> state_column);
        END IF;
        IF reason IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = format('la tabla %I.%I es de solo anexar: %s (P4)',
                                 TG_TABLE_SCHEMA, TG_TABLE_NAME, reason);
        END IF;
        RETURN NEW;
    END
    $$
    """,
)

_TABLES = (
    # §2.1 ZoneCatalogVersion ⛓
    f"""
    CREATE TABLE catalog.zone_catalog_version (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        catalog_version integer NOT NULL
            CONSTRAINT zone_catalog_version_version CHECK (catalog_version >= 1),
        issued_at timestamptz NOT NULL,
        issued_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        role_in_use text NOT NULL
            CONSTRAINT zone_catalog_version_role_values CHECK (role_in_use IN ({_ROLES})),
        reason_es text NOT NULL
            CONSTRAINT zone_catalog_version_reason_length
                CHECK (char_length(reason_es) BETWEEN 10 AND 500),
        changed_fields text[] NOT NULL
            CONSTRAINT zone_catalog_version_changed_fields CHECK (
                cardinality(changed_fields) BETWEEN 1 AND 8
                AND changed_fields <@ {_CHANGED_FIELDS}
                AND catalog.text_array_is_set(changed_fields)
            ),
        payload jsonb NOT NULL
            CONSTRAINT zone_catalog_version_payload_object CHECK (jsonb_typeof(payload) = 'object'),
        envelope jsonb NOT NULL
            CONSTRAINT zone_catalog_version_envelope_object
                CHECK (jsonb_typeof(envelope) = 'object'),
        single_occupancy boolean NOT NULL,
        aggregation_window_minutes integer NOT NULL DEFAULT 60
            CONSTRAINT zone_catalog_version_aggregation_window
                CHECK (aggregation_window_minutes BETWEEN 15 AND 480),
        ledger_record_id uuid NOT NULL,
        superseded_at timestamptz,
        PRIMARY KEY (zone_id, catalog_version),
        CONSTRAINT zone_catalog_version_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT zone_catalog_version_superseded_after_issue CHECK (superseded_at >= issued_at)
    )
    """,
    """
    CREATE INDEX zone_catalog_version_scope
        ON catalog.zone_catalog_version (organization_id, plant_id, zone_id, catalog_version)
    """,
    # §2.2 DeclaredStandardVersion ⛓. Una versión vigente por estándar (índice parcial: la
    # anterior se retira en la misma transacción) y un estándar en una sola zona (exclusión).
    f"""
    CREATE TABLE catalog.declared_standard_version (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        standard_id uuid NOT NULL,
        version integer NOT NULL CONSTRAINT declared_standard_version_version CHECK (version >= 1),
        family text NOT NULL
            CONSTRAINT declared_standard_version_family_values
                CHECK (family IN ({_PREDICATE_FAMILY})),
        title_es text NOT NULL
            CONSTRAINT declared_standard_version_title_length
                CHECK (char_length(title_es) BETWEEN 1 AND 120),
        declared_text text NOT NULL
            CONSTRAINT declared_standard_version_declared_text_length
                CHECK (char_length(declared_text) BETWEEN 1 AND 4000),
        declared_by jsonb NOT NULL
            CONSTRAINT declared_standard_version_declared_by_object
                CHECK (jsonb_typeof(declared_by) = 'object'),
        effective_from timestamptz NOT NULL,
        tier_policy text NOT NULL DEFAULT 'tier_1_when_signal_valid'
            CONSTRAINT declared_standard_version_tier_policy
                CHECK (tier_policy = 'tier_1_when_signal_valid'),
        predicate jsonb NOT NULL
            CONSTRAINT declared_standard_version_predicate_object
                CHECK (jsonb_typeof(predicate) = 'object'),
        catalog_version integer NOT NULL
            CONSTRAINT declared_standard_version_catalog_version CHECK (catalog_version >= 1),
        retired_in_catalog_version integer,
        reason_es text NOT NULL
            CONSTRAINT declared_standard_version_reason_length
                CHECK (char_length(reason_es) BETWEEN 10 AND 500),
        PRIMARY KEY (standard_id, version),
        CONSTRAINT declared_standard_version_zone_fkey
            FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT declared_standard_version_catalog_fkey FOREIGN KEY (zone_id, catalog_version)
            REFERENCES catalog.zone_catalog_version (zone_id, catalog_version),
        CONSTRAINT declared_standard_version_retired_after_birth
            CHECK (retired_in_catalog_version > catalog_version),
        CONSTRAINT declared_standard_version_one_zone
            EXCLUDE USING gist (standard_id WITH =, zone_id WITH <>)
    )
    """,
    """
    CREATE UNIQUE INDEX declared_standard_version_one_current
        ON catalog.declared_standard_version (standard_id)
        WHERE retired_in_catalog_version IS NULL
    """,
    """
    CREATE INDEX declared_standard_version_scope
        ON catalog.declared_standard_version (organization_id, plant_id, zone_id, catalog_version)
    """,
    # Nota de §3.14 ZoneCamera 🔒: la historia vive en las versiones del catálogo.
    """
    CREATE TABLE catalog.zone_camera (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        camera_id uuid NOT NULL,
        role_in_zone text NOT NULL
            CONSTRAINT zone_camera_role_values CHECK (role_in_zone IN ('primary', 'redundant')),
        declared_min_fps double precision NOT NULL
            CONSTRAINT zone_camera_declared_min_fps CHECK (declared_min_fps BETWEEN 1 AND 60),
        stream_reference text NOT NULL
            CONSTRAINT zone_camera_stream_reference_format
                CHECK (stream_reference ~ '^[a-z][a-z0-9_.-]{0,63}$'),
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (zone_id, camera_id),
        CONSTRAINT zone_camera_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id)
    )
    """,
    "CREATE INDEX zone_camera_scope ON catalog.zone_camera (organization_id, plant_id, zone_id)",
    # §2.3 FamilyAdmission ⛓: admitida si y solo si las tres respuestas son afirmativas; el
    # criterio fallido es una respuesta negativa. Una admisión por (planta, familia).
    f"""
    CREATE TABLE catalog.family_admission (
        admission_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        family text NOT NULL
            CONSTRAINT family_admission_family_values CHECK (family IN ({_PREDICATE_FAMILY})),
        answers jsonb NOT NULL
            CONSTRAINT family_admission_answers_shape CHECK (
                jsonb_typeof(answers) = 'object'
                AND COALESCE(jsonb_typeof(answers -> 'standard'), '') = 'boolean'
                AND COALESCE(jsonb_typeof(answers -> 'remedy'), '') = 'boolean'
                AND COALESCE(jsonb_typeof(answers -> 'subject'), '') = 'boolean'
                AND answers - ARRAY['standard', 'remedy', 'subject'] = '{{}}'::jsonb
            ),
        justification_es text
            CONSTRAINT family_admission_justification_length
                CHECK (char_length(justification_es) BETWEEN 1 AND 2000),
        result text NOT NULL
            CONSTRAINT family_admission_result_values CHECK (result IN ('admitted', 'rejected')),
        failed_criterion text
            CONSTRAINT family_admission_failed_criterion_values
                CHECK (failed_criterion IN ('standard', 'remedy', 'subject')),
        evaluated_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        role_in_use text NOT NULL
            CONSTRAINT family_admission_role_values CHECK (role_in_use IN ({_ROLES})),
        evaluated_at timestamptz NOT NULL,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT family_admission_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT family_admission_result_matches_answers CHECK (
            (result = 'admitted') = (
                (answers ->> 'standard')::boolean
                AND (answers ->> 'remedy')::boolean
                AND (answers ->> 'subject')::boolean
            )
        ),
        CONSTRAINT family_admission_failed_criterion_iff_rejected
            CHECK ((result = 'rejected') = (failed_criterion IS NOT NULL)),
        CONSTRAINT family_admission_failed_criterion_is_negative
            CHECK (failed_criterion IS NULL OR answers ->> failed_criterion = 'false')
    )
    """,
    """
    CREATE UNIQUE INDEX family_admission_admitted_once
        ON catalog.family_admission (organization_id, plant_id, family)
        WHERE result = 'admitted'
    """,
    """
    CREATE INDEX family_admission_scope
        ON catalog.family_admission (organization_id, plant_id, family, evaluated_at)
    """,
    # §2.4 ZoneGateState 🔒: una fila por zona; resulting_mode es función total (BR-CTR-40).
    f"""
    CREATE TABLE catalog.zone_gate_state (
        zone_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        mounting jsonb NOT NULL
            CONSTRAINT zone_gate_state_mounting_shape CHECK (
                jsonb_typeof(mounting) = 'object'
                AND COALESCE(mounting ->> 'status', '') IN ({_GATE_STATUS})
            ),
        usage jsonb NOT NULL
            CONSTRAINT zone_gate_state_usage_shape CHECK (
                jsonb_typeof(usage) = 'object'
                AND COALESCE(usage ->> 'status', '') IN ({_GATE_STATUS})
            ),
        resulting_mode text NOT NULL
            CONSTRAINT zone_gate_state_resulting_mode_values
                CHECK (resulting_mode IN ('no_capture', 'commissioning', 'productive')),
        issued_at timestamptz NOT NULL,
        envelope jsonb NOT NULL
            CONSTRAINT zone_gate_state_envelope_object CHECK (jsonb_typeof(envelope) = 'object'),
        valid_until timestamptz NOT NULL,
        CONSTRAINT zone_gate_state_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT zone_gate_state_resulting_mode_derived CHECK (
            resulting_mode = CASE
                WHEN mounting ->> 'status' <> 'approved' THEN 'no_capture'
                WHEN usage ->> 'status' <> 'approved' THEN 'commissioning'
                ELSE 'productive'
            END
        ),
        CONSTRAINT zone_gate_state_validity CHECK (valid_until = issued_at + interval '7 days')
    )
    """,
    "CREATE INDEX zone_gate_state_scope ON catalog.zone_gate_state (organization_id, plant_id)",
    # §2.5 GateStateHistory ⛓: rango generado y exclusión GiST (PAT-GOB-REN-01, R-GOB-14).
    f"""
    CREATE TABLE catalog.gate_state_history (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        gate text NOT NULL
            CONSTRAINT gate_state_history_gate_values CHECK (gate IN ('mounting', 'usage')),
        status text NOT NULL
            CONSTRAINT gate_state_history_status_values CHECK (status IN ({_GATE_STATUS})),
        effective_from timestamptz NOT NULL,
        effective_until timestamptz,
        effective tstzrange NOT NULL
            GENERATED ALWAYS AS (tstzrange(effective_from, effective_until, '[)')) STORED,
        decided_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        reason_es text
            CONSTRAINT gate_state_history_reason_length
                CHECK (char_length(reason_es) BETWEEN 10 AND 500),
        ledger_record_id uuid NOT NULL,
        -- Diferible (al final de la sentencia): dos intervalos que empiezan en el mismo instante
        -- se solapan y se rechazan siempre como exclusion_violation, nunca como clave duplicada.
        CONSTRAINT gate_state_history_pkey PRIMARY KEY (zone_id, gate, effective_from)
            DEFERRABLE INITIALLY IMMEDIATE,
        CONSTRAINT gate_state_history_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT gate_state_history_interval CHECK (effective_until > effective_from),
        CONSTRAINT gate_state_history_reason_iff_revoked
            CHECK ((status = 'revoked') = (reason_es IS NOT NULL)),
        CONSTRAINT gate_state_no_overlap
            EXCLUDE USING gist (zone_id WITH =, gate WITH =, effective WITH &&)
    )
    """,
    # state_at (effective @> t) y gate_history (effective && [from, to)) con el alcance delante;
    # el índice de gate_state_no_overlap sirve las mismas consultas por zona.
    """
    CREATE INDEX gate_state_history_scope_effective
        ON catalog.gate_state_history USING gist (organization_id, plant_id, zone_id, effective)
    """,
    # §2.6 MountingGateRecord ⛓ (con la nota D-2: la comprobación automática pasa al acta).
    f"""
    CREATE TABLE catalog.mounting_gate_record (
        record_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        scope_text_es text NOT NULL
            CONSTRAINT mounting_gate_record_scope_text_length
                CHECK (char_length(scope_text_es) BETWEEN 1 AND 4000),
        cameras jsonb NOT NULL
            CONSTRAINT mounting_gate_record_cameras_shape CHECK (
                CASE WHEN jsonb_typeof(cameras) = 'array'
                     THEN jsonb_array_length(cameras) BETWEEN 1 AND 8 ELSE false END
            ),
        blur_verification jsonb NOT NULL
            CONSTRAINT mounting_gate_record_blur_verification_object
                CHECK (jsonb_typeof(blur_verification) = 'object'),
        document_ref jsonb
            CONSTRAINT mounting_gate_record_document_ref_object
                CHECK (jsonb_typeof(document_ref) = 'object'),
        signed_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        role_in_use text NOT NULL
            CONSTRAINT mounting_gate_record_role_values CHECK (role_in_use IN ({_ROLES})),
        plant_policy_loaded_at_signing boolean NOT NULL,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT mounting_gate_record_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id)
    )
    """,
    """
    CREATE INDEX mounting_gate_record_scope
        ON catalog.mounting_gate_record (organization_id, plant_id, zone_id)
    """,
    # §2.7 PlantSignatoryPolicy 🔒: mínimo tres y siempre copasst (RF-PLA-09, H-50).
    f"""
    CREATE TABLE catalog.plant_signatory_policy (
        plant_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        required_roles text[] NOT NULL
            CONSTRAINT plant_signatory_policy_required_roles CHECK (
                'copasst' = ANY (required_roles)
                AND required_roles <@ ARRAY[{_ROLES}]::text[]
                AND catalog.text_array_is_set(required_roles)
            ),
        minimum integer NOT NULL CONSTRAINT plant_signatory_policy_minimum CHECK (minimum >= 3),
        workers_role text NOT NULL DEFAULT 'copasst'
            CONSTRAINT plant_signatory_policy_workers_role CHECK (workers_role = 'copasst'),
        updated_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        updated_at timestamptz NOT NULL,
        CONSTRAINT plant_signatory_policy_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT plant_signatory_policy_scope UNIQUE (organization_id, plant_id)
    )
    """,
    # §2.8 UseAgreement ⛓: pending_signatures → approved → (superseded | revoked), solo hacia
    # adelante (business-logic-model §3.2); cada estado fija sus cierres.
    """
    CREATE TABLE catalog.use_agreement (
        agreement_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'pending_signatures'
            CONSTRAINT use_agreement_status_values CHECK (
                status IN ('pending_signatures', 'approved', 'superseded', 'revoked')
            ),
        signatories jsonb NOT NULL
            CONSTRAINT use_agreement_signatories_shape CHECK (
                CASE WHEN jsonb_typeof(signatories) = 'array'
                     THEN jsonb_array_length(signatories) >= 3 ELSE false END
            ),
        document_ref jsonb
            CONSTRAINT use_agreement_document_ref_object
                CHECK (jsonb_typeof(document_ref) = 'object'),
        replaces_agreement_id uuid REFERENCES catalog.use_agreement (agreement_id),
        created_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        created_at timestamptz NOT NULL,
        approved_at timestamptz,
        approved_by uuid REFERENCES identity.user_account (user_id),
        ledger_record_id uuid,
        superseded_at timestamptz,
        revoked_at timestamptz,
        CONSTRAINT use_agreement_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT use_agreement_scope UNIQUE (organization_id, plant_id, agreement_id),
        CONSTRAINT use_agreement_not_self_replacing CHECK (replaces_agreement_id <> agreement_id),
        CONSTRAINT use_agreement_approval_complete CHECK (
            (status = 'pending_signatures') = (approved_at IS NULL)
            AND (approved_at IS NULL) = (approved_by IS NULL)
            AND (approved_at IS NULL) = (ledger_record_id IS NULL)
        ),
        CONSTRAINT use_agreement_superseded_has_date
            CHECK ((status = 'superseded') = (superseded_at IS NOT NULL)),
        CONSTRAINT use_agreement_revoked_has_date
            CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
        CONSTRAINT use_agreement_order CHECK (
            approved_at >= created_at AND superseded_at >= approved_at AND revoked_at >= approved_at
        )
    )
    """,
    """
    CREATE INDEX use_agreement_scope_zone
        ON catalog.use_agreement (organization_id, plant_id, zone_id, status)
    """,
    # §2.9 AgreementConfirmation ⛓: un firmante confirma una vez por acuerdo.
    f"""
    CREATE TABLE catalog.agreement_confirmation (
        agreement_id uuid NOT NULL,
        user_id uuid NOT NULL REFERENCES identity.user_account (user_id),
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        role_in_use text NOT NULL
            CONSTRAINT agreement_confirmation_role_values CHECK (role_in_use IN ({_ROLES})),
        confirmed_at timestamptz NOT NULL,
        origin text NOT NULL
            CONSTRAINT agreement_confirmation_origin_values
                CHECK (origin IN ('management', 'transparency')),
        PRIMARY KEY (agreement_id, user_id),
        CONSTRAINT agreement_confirmation_agreement_fkey
            FOREIGN KEY (organization_id, plant_id, agreement_id)
            REFERENCES catalog.use_agreement (organization_id, plant_id, agreement_id)
    )
    """,
    """
    CREATE INDEX agreement_confirmation_scope
        ON catalog.agreement_confirmation (organization_id, plant_id, agreement_id)
    """,
    # §2.10 PlantPolicy ⛓: versión monótona por planta; una nueva no borra la anterior.
    """
    CREATE TABLE catalog.plant_policy (
        policy_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        version integer NOT NULL CONSTRAINT plant_policy_version CHECK (version >= 1),
        signed_at timestamptz NOT NULL,
        signed_by_display_name text NOT NULL
            CONSTRAINT plant_policy_signed_by_length
                CHECK (char_length(signed_by_display_name) BETWEEN 1 AND 120),
        legal_opinion_reference text NOT NULL
            CONSTRAINT plant_policy_legal_opinion_length
                CHECK (char_length(legal_opinion_reference) BETWEEN 1 AND 120),
        document_ref jsonb NOT NULL
            CONSTRAINT plant_policy_document_ref_object
                CHECK (jsonb_typeof(document_ref) = 'object'),
        criteria_summary_es text NOT NULL
            CONSTRAINT plant_policy_criteria_summary_length
                CHECK (char_length(criteria_summary_es) BETWEEN 1 AND 2000),
        loaded_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        loaded_at timestamptz NOT NULL,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT plant_policy_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT plant_policy_version_unique UNIQUE (plant_id, version)
    )
    """,
    """
    CREATE INDEX plant_policy_scope ON catalog.plant_policy (organization_id, plant_id, version)
    """,
    # §3.14 DocumentUploadGrant 🔒, en catalog por la nota del 2026-09-23 (LC-GOB-05): clave
    # org/{organization_id}/plant/{plant_id}/documents/{document_id}.{ext} (infra §4.1), hasta
    # 20 MB (NFR-GOB-23) y vencimiento de 15 minutos como mucho.
    """
    CREATE TABLE catalog.document_upload_grant (
        document_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        kind text NOT NULL
            CONSTRAINT document_upload_grant_kind_values CHECK (
                kind IN ('scope_record', 'use_agreement', 'plant_policy', 'blur_check_capture')
            ),
        content_type text NOT NULL
            CONSTRAINT document_upload_grant_content_type_values
                CHECK (content_type IN ('application/pdf', 'image/jpeg', 'image/png')),
        storage_key text NOT NULL
            CONSTRAINT document_upload_grant_storage_key_format CHECK (
                char_length(storage_key) <= 512
                AND storage_key ~ (
                    '^org/' || organization_id::text || '/plant/' || plant_id::text
                    || '/documents/' || document_id::text || '[.][a-z0-9]{1,8}$'
                )
            ),
        sha256 text NOT NULL
            CONSTRAINT document_upload_grant_sha256_format CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        size_bytes bigint NOT NULL
            CONSTRAINT document_upload_grant_size CHECK (size_bytes BETWEEN 1 AND 20971520),
        issued_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'issued'
            CONSTRAINT document_upload_grant_status_values
                CHECK (status IN ('issued', 'used', 'expired', 'orphan')),
        CONSTRAINT document_upload_grant_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT document_upload_grant_expiry CHECK (
            expires_at > issued_at AND expires_at <= issued_at + interval '15 minutes'
        )
    )
    """,
    """
    CREATE INDEX document_upload_grant_scope
        ON catalog.document_upload_grant (organization_id, plant_id, status, expires_at)
    """,
    # §2.11 WalkTestSession 🔒: una sola sesión abierta (en curso o reabierta) por zona.
    """
    CREATE TABLE catalog.walk_test_session (
        session_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        catalog_version integer NOT NULL
            CONSTRAINT walk_test_session_catalog_version CHECK (catalog_version >= 1),
        kind text NOT NULL
            CONSTRAINT walk_test_session_kind_values
                CHECK (kind IN ('initial', 'regression_rerun')),
        status text NOT NULL DEFAULT 'in_progress'
            CONSTRAINT walk_test_session_status_values
                CHECK (status IN ('in_progress', 'closed', 'incomplete', 'reopened')),
        passes_per_cell integer NOT NULL
            CONSTRAINT walk_test_session_passes_per_cell CHECK (passes_per_cell >= 3),
        matrix_rows jsonb NOT NULL
            CONSTRAINT walk_test_session_matrix_rows_array
                CHECK (jsonb_typeof(matrix_rows) = 'array'),
        started_at timestamptz NOT NULL,
        last_activity_at timestamptz NOT NULL,
        closed_at timestamptz,
        commissioning_record_id uuid,
        CONSTRAINT walk_test_session_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT walk_test_session_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT walk_test_session_catalog_fkey FOREIGN KEY (zone_id, catalog_version)
            REFERENCES catalog.zone_catalog_version (zone_id, catalog_version),
        CONSTRAINT walk_test_session_scope UNIQUE (organization_id, plant_id, session_id),
        CONSTRAINT walk_test_session_activity_order CHECK (last_activity_at >= started_at),
        CONSTRAINT walk_test_session_closed_has_date
            CHECK ((status = 'closed') = (closed_at IS NOT NULL) AND closed_at >= started_at),
        CONSTRAINT walk_test_session_record_only_when_closed
            CHECK (commissioning_record_id IS NULL OR status = 'closed')
    )
    """,
    """
    CREATE UNIQUE INDEX walk_test_session_one_open_per_zone
        ON catalog.walk_test_session (zone_id) WHERE status IN ('in_progress', 'reopened')
    """,
    """
    CREATE INDEX walk_test_session_scope_zone
        ON catalog.walk_test_session (organization_id, plant_id, zone_id, status)
    """,
    # §2.12 WalkTestStep ⛓. Sin índice por responsable: no existe agregación de horas por
    # persona (H-53, prohibición estructural).
    """
    CREATE TABLE catalog.walk_test_step (
        step_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        session_id uuid NOT NULL,
        step_kind text NOT NULL
            CONSTRAINT walk_test_step_kind_values CHECK (step_kind IN (
                'physical_setup', 'signal_mapping', 'framing', 'walk_test_passes',
                'occlusion_test', 'latency_measurement', 'review_and_signatures', 'other'
            )),
        responsible_user_id uuid NOT NULL REFERENCES identity.user_account (user_id),
        started_at timestamptz NOT NULL,
        ended_at timestamptz,
        correction jsonb
            CONSTRAINT walk_test_step_correction_object CHECK (jsonb_typeof(correction) = 'object'),
        CONSTRAINT walk_test_step_session_fkey FOREIGN KEY (organization_id, plant_id, session_id)
            REFERENCES catalog.walk_test_session (organization_id, plant_id, session_id),
        CONSTRAINT walk_test_step_interval CHECK (ended_at >= started_at)
    )
    """,
    """
    CREATE INDEX walk_test_step_scope
        ON catalog.walk_test_step (organization_id, plant_id, session_id)
    """,
    # §2.13 WalkTestPass ⛓
    """
    CREATE TABLE catalog.walk_test_pass (
        pass_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        session_id uuid NOT NULL,
        row_id uuid NOT NULL,
        result text NOT NULL
            CONSTRAINT walk_test_pass_result_values
                CHECK (result IN ('detected', 'missed', 'false_alarm')),
        evidence_ref uuid,
        recorded_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        recorded_at timestamptz NOT NULL,
        CONSTRAINT walk_test_pass_session_fkey FOREIGN KEY (organization_id, plant_id, session_id)
            REFERENCES catalog.walk_test_session (organization_id, plant_id, session_id)
    )
    """,
    """
    CREATE INDEX walk_test_pass_scope
        ON catalog.walk_test_pass (organization_id, plant_id, session_id, row_id)
    """,
    # §2.14 OcclusionTest ⛓ con la nota del 2026-09-20: deadline = ended_at + 5 min y pending,
    # que pasa una sola vez a verified, failed o declared.
    """
    CREATE TABLE catalog.occlusion_test (
        test_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        session_id uuid NOT NULL,
        camera_id uuid NOT NULL,
        started_at timestamptz NOT NULL,
        ended_at timestamptz NOT NULL,
        deadline timestamptz NOT NULL,
        verification text NOT NULL DEFAULT 'pending'
            CONSTRAINT occlusion_test_verification_values
                CHECK (verification IN ('pending', 'verified', 'declared', 'failed')),
        correlated_event_ids uuid[]
            CONSTRAINT occlusion_test_correlated_event_ids_shape CHECK (
                array_ndims(correlated_event_ids) = 1
                AND array_position(correlated_event_ids, NULL) IS NULL
            ),
        declared_reason_es text
            CONSTRAINT occlusion_test_declared_reason_length
                CHECK (char_length(declared_reason_es) BETWEEN 10 AND 500),
        recorded_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        ledger_record_id uuid,
        CONSTRAINT occlusion_test_session_fkey FOREIGN KEY (organization_id, plant_id, session_id)
            REFERENCES catalog.walk_test_session (organization_id, plant_id, session_id),
        CONSTRAINT occlusion_test_window CHECK (ended_at >= started_at),
        CONSTRAINT occlusion_test_deadline CHECK (deadline = ended_at + interval '5 minutes'),
        CONSTRAINT occlusion_test_verified_has_events
            CHECK (verification <> 'verified' OR correlated_event_ids IS NOT NULL),
        CONSTRAINT occlusion_test_reason_iff_declared
            CHECK ((verification = 'declared') = (declared_reason_es IS NOT NULL)),
        CONSTRAINT occlusion_test_result_recorded
            CHECK (verification = 'pending' OR ledger_record_id IS NOT NULL)
    )
    """,
    """
    CREATE INDEX occlusion_test_scope
        ON catalog.occlusion_test (organization_id, plant_id, session_id, camera_id)
    """,
    # §2.15 CommissioningRecord ⛓: solo se escribe al cerrar, con cero falsos negativos (H-51);
    # un acta por sesión. installer_measurements: tramo 1 y líneas base (notas del 2026-09-23).
    """
    CREATE TABLE catalog.commissioning_record (
        commissioning_record_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        session_id uuid NOT NULL,
        catalog_version integer NOT NULL
            CONSTRAINT commissioning_record_catalog_version CHECK (catalog_version >= 1),
        matrix_results jsonb NOT NULL
            CONSTRAINT commissioning_record_matrix_results_array
                CHECK (jsonb_typeof(matrix_results) = 'array'),
        false_negatives_total integer NOT NULL
            CONSTRAINT commissioning_record_no_false_negatives CHECK (false_negatives_total = 0),
        false_alarm_rate_observed numeric NOT NULL
            CONSTRAINT commissioning_record_false_alarm_rate CHECK (false_alarm_rate_observed >= 0),
        false_alarm_threshold numeric NOT NULL
            CONSTRAINT commissioning_record_false_alarm_threshold
                CHECK (false_alarm_threshold >= 0),
        false_alarm_acceptance jsonb
            CONSTRAINT commissioning_record_false_alarm_acceptance_object
                CHECK (jsonb_typeof(false_alarm_acceptance) = 'object'),
        latency jsonb NOT NULL
            CONSTRAINT commissioning_record_latency_object CHECK (jsonb_typeof(latency) = 'object'),
        installer_measurements jsonb NOT NULL
            CONSTRAINT commissioning_record_installer_measurements_object
                CHECK (jsonb_typeof(installer_measurements) = 'object'),
        cameras_measured jsonb NOT NULL
            CONSTRAINT commissioning_record_cameras_measured_array
                CHECK (jsonb_typeof(cameras_measured) = 'array'),
        occlusion_summary jsonb NOT NULL
            CONSTRAINT commissioning_record_occlusion_summary_array
                CHECK (jsonb_typeof(occlusion_summary) = 'array'),
        total_hours numeric NOT NULL
            CONSTRAINT commissioning_record_total_hours CHECK (total_hours >= 0),
        steps_summary jsonb NOT NULL
            CONSTRAINT commissioning_record_steps_summary_array
                CHECK (jsonb_typeof(steps_summary) = 'array'),
        signatures jsonb NOT NULL
            CONSTRAINT commissioning_record_signatures_array
                CHECK (jsonb_typeof(signatures) = 'array'),
        closed_at timestamptz NOT NULL,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT commissioning_record_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT commissioning_record_session_fkey
            FOREIGN KEY (organization_id, plant_id, session_id)
            REFERENCES catalog.walk_test_session (organization_id, plant_id, session_id),
        CONSTRAINT commissioning_record_one_per_session UNIQUE (session_id),
        CONSTRAINT commissioning_record_false_alarms_accepted CHECK (
            false_alarm_rate_observed <= false_alarm_threshold
            OR false_alarm_acceptance IS NOT NULL
        )
    )
    """,
    """
    CREATE INDEX commissioning_record_scope
        ON catalog.commissioning_record (organization_id, plant_id, zone_id, closed_at)
    """,
    # §2.16 WalkTestRegression 🔒: una fila por zona; marca sin bloqueo operativo.
    """
    CREATE TABLE catalog.walk_test_regression (
        zone_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        state text NOT NULL
            CONSTRAINT walk_test_regression_state_values CHECK (state IN ('current', 'pending')),
        marked_at timestamptz,
        cause text
            CONSTRAINT walk_test_regression_cause_values CHECK (
                cause IN ('catalog_change', 'model_version_change', 'framing_recaptured')
            ),
        catalog_version integer
            CONSTRAINT walk_test_regression_catalog_version CHECK (catalog_version >= 1),
        model_version text
            CONSTRAINT walk_test_regression_model_version_length
                CHECK (char_length(model_version) BETWEEN 1 AND 64),
        affected_row_ids jsonb
            CONSTRAINT walk_test_regression_affected_rows_shape CHECK (
                jsonb_typeof(affected_row_ids) = 'array' OR affected_row_ids = '"all"'::jsonb
            ),
        cleared_at timestamptz,
        cleared_by_session_id uuid,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT walk_test_regression_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT walk_test_regression_pending_complete CHECK (
            state <> 'pending'
            OR (marked_at IS NOT NULL AND cause IS NOT NULL AND affected_row_ids IS NOT NULL)
        ),
        CONSTRAINT walk_test_regression_clearance_complete
            CHECK ((cleared_at IS NULL) = (cleared_by_session_id IS NULL))
    )
    """,
    """
    CREATE INDEX walk_test_regression_scope
        ON catalog.walk_test_regression (organization_id, plant_id, state)
    """,
)

TABLES = (
    "zone_catalog_version",
    "declared_standard_version",
    "zone_camera",
    "family_admission",
    "zone_gate_state",
    "gate_state_history",
    "mounting_gate_record",
    "plant_signatory_policy",
    "use_agreement",
    "agreement_confirmation",
    "plant_policy",
    "document_upload_grant",
    "walk_test_session",
    "walk_test_step",
    "walk_test_pass",
    "occlusion_test",
    "commissioning_record",
    "walk_test_regression",
)
"""Las 18 tablas de ``catalog``: todas con organización y planta, RLS forzada y política de
proveedor."""

_ENTITIES = (
    "ZoneCatalogVersion",
    "DeclaredStandardVersion",
    "ZoneCamera",
    "FamilyAdmission",
    "ZoneGateState",
    "GateStateHistory",
    "MountingGateRecord",
    "PlantSignatoryPolicy",
    "UseAgreement",
    "AgreementConfirmation",
    "PlantPolicy",
    "DocumentUploadGrant",
    "WalkTestSession",
    "WalkTestStep",
    "WalkTestPass",
    "OcclusionTest",
    "CommissioningRecord",
    "WalkTestRegression",
)

APPEND_ONLY_TABLES = (
    "zone_catalog_version",
    "declared_standard_version",
    "family_admission",
    "gate_state_history",
    "mounting_gate_record",
    "use_agreement",
    "agreement_confirmation",
    "plant_policy",
    "walk_test_step",
    "walk_test_pass",
    "occlusion_test",
    "commissioning_record",
)
"""Tablas ⛓ (``migrations/append_only.py``)."""

CLOSING_COLUMNS: dict[str, tuple[str, ...]] = {
    "zone_catalog_version": ("superseded_at",),
    "declared_standard_version": ("retired_in_catalog_version",),
    "gate_state_history": ("effective_until",),
    "use_agreement": (
        "approved_at",
        "approved_by",
        "ledger_record_id",
        "superseded_at",
        "revoked_at",
    ),
    "walk_test_step": ("ended_at", "correction"),
    "occlusion_test": ("correlated_event_ids", "declared_reason_es", "ledger_record_id"),
}
"""Lista blanca de cierres de cada tabla ⛓: de nulo a su valor una sola vez. Las ⛓ que no
aparecen no admiten ningún ``UPDATE``."""

STATE_TRANSITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "use_agreement": (
        "status",
        ("pending_signatures>approved", "approved>superseded", "approved>revoked"),
    ),
    "occlusion_test": (
        "verification",
        ("pending>verified", "pending>failed", "pending>declared"),
    ),
    "document_upload_grant": ("status", ("issued>used", "issued>expired")),
}
"""Columna de estado y transiciones admitidas (solo hacia adelante)."""

DERIVED_COLUMNS: dict[str, tuple[str, ...]] = {"gate_state_history": ("effective",)}
"""Columnas generadas: cambian con su cierre y la guarda no las compara."""

_MUTABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "zone_camera": ("role_in_zone", "declared_min_fps", "stream_reference", "updated_at"),
    "zone_gate_state": (
        "mounting",
        "usage",
        "resulting_mode",
        "issued_at",
        "envelope",
        "valid_until",
    ),
    "plant_signatory_policy": ("required_roles", "minimum", "updated_by", "updated_at"),
    "walk_test_session": ("status", "last_activity_at", "closed_at", "commissioning_record_id"),
    "walk_test_regression": (
        "state",
        "marked_at",
        "cause",
        "catalog_version",
        "model_version",
        "affected_row_ids",
        "cleared_at",
        "cleared_by_session_id",
        "ledger_record_id",
    ),
}
"""Proyecciones 🔒: columnas que el diseño deja cambiar (claves, organización y planta, nunca)."""


def app_updatable_columns(table: str) -> tuple[str, ...]:
    """Columnas con ``UPDATE`` de ``vigia_app``: la lista blanca o las mutables de la tabla."""
    if table in _MUTABLE_COLUMNS:
        return _MUTABLE_COLUMNS[table]
    state = STATE_TRANSITIONS.get(table)
    return CLOSING_COLUMNS.get(table, ()) + ((state[0],) if state else ())


def _array(items: tuple[str, ...]) -> str:
    return "{" + ",".join(items) + "}"


_ORGANIZATION_POLICY = """
CREATE POLICY organization_isolation ON catalog.{table} AS PERMISSIVE FOR ALL TO PUBLIC
    USING (organization_id = shared.vigia_current_organization())
    WITH CHECK (organization_id = shared.vigia_current_organization())
"""

_PROVIDER_POLICY = """
CREATE POLICY provider_concession_scope ON catalog.{table} AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (identity.rls_provider_scope_allows(organization_id, plant_id))
    WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))
"""


def _row_security() -> list[str]:
    statements: list[str] = []
    for table in TABLES:
        statements += [
            f"ALTER TABLE catalog.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE catalog.{table} FORCE ROW LEVEL SECURITY",
            _ORGANIZATION_POLICY.format(table=table),
            _PROVIDER_POLICY.format(table=table),
        ]
    return statements


def _update_guard(table: str) -> str:
    state, transitions = STATE_TRANSITIONS.get(table, ("", ()))
    arguments = (
        _array(CLOSING_COLUMNS.get(table, ())),
        state,
        _array(transitions),
        _array(DERIVED_COLUMNS.get(table, ())),
    )
    return "catalog.guard_update(" + ", ".join(f"'{argument}'" for argument in arguments) + ")"


def _triggers() -> list[str]:
    statements: list[str] = []
    for table in APPEND_ONLY_TABLES:
        guarded = table in CLOSING_COLUMNS or table in STATE_TRANSITIONS
        update = _update_guard(table) if guarded else "shared.vigia_reject_mutation()"
        statements += [
            f"CREATE TRIGGER append_only_update BEFORE UPDATE ON catalog.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {update}",
            f"CREATE TRIGGER append_only_delete BEFORE DELETE ON catalog.{table}"
            " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
            f"CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON catalog.{table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()",
        ]
        for trigger in ("append_only_update", "append_only_delete", "append_only_no_truncate"):
            statements.append(f"ALTER TABLE catalog.{table} ENABLE ALWAYS TRIGGER {trigger}")
    statements += [
        "CREATE TRIGGER state_transition BEFORE UPDATE ON catalog.document_upload_grant"
        f" FOR EACH ROW EXECUTE FUNCTION {_update_guard('document_upload_grant')}",
        "ALTER TABLE catalog.document_upload_grant ENABLE ALWAYS TRIGGER state_transition",
    ]
    return statements


def _grants() -> list[str]:
    statements = [
        "GRANT USAGE ON SCHEMA catalog TO vigia_app",
        "REVOKE ALL ON FUNCTION catalog.guard_update() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION catalog.text_array_is_set(text[]) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION catalog.text_array_is_set(text[]) TO vigia_app",
    ]
    for table in TABLES:
        statements.append(f"GRANT SELECT, INSERT ON catalog.{table} TO vigia_app")
        columns = app_updatable_columns(table)
        if columns:
            statements.append(
                f"GRANT UPDATE ({', '.join(columns)}) ON catalog.{table} TO vigia_app"
            )
    return statements


def _comments() -> list[str]:
    statements = [
        "COMMENT ON SCHEMA catalog IS"
        " 'Módulo catalog de U-03: catálogo, compuertas, acuerdos y comisionamiento'",
        "COMMENT ON CONSTRAINT gate_state_no_overlap ON catalog.gate_state_history IS"
        " 'Ningún solapamiento de intervalos de la misma zona y compuerta (PR-GOB-21, R-GOB-14)'",
    ]
    for table, entity in zip(TABLES, _ENTITIES, strict=True):
        mark = " ⛓ solo anexar" if table in APPEND_ONLY_TABLES else ""
        statements.append(
            f"COMMENT ON TABLE catalog.{table} IS '{entity} (domain-entities §2){mark};"
            " seguridad a nivel de fila forzada por organización y concesión de proveedor'"
        )
    return statements


def upgrade() -> None:
    # Idempotente: nuc_0001 ya la crea (adenda A-32). R-GOB-14: gate_state_no_overlap la exige.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")
    # Como en nuc_0004: todo lo creado es de vigia_migrate, lo ejecute el maestro o él.
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute("CREATE SCHEMA catalog AUTHORIZATION vigia_migrate")
    for statement in (
        *_FUNCTIONS,
        *_TABLES,
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
