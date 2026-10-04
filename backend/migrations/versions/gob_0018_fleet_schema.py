"""Esquema ``fleet``: identidad, credenciales, inventario, latidos, alarmas y concesiones
(TASK-203, LC-GOB-21 parte 2, PAT-GOB-ESC-02, PR-GOB-26).

Revisión gob_0018. Las tablas de ``domain-entities.md`` §3 y sus notas fechadas, con el
aislamiento de U-02 y la regla de solo anexar de gob_0017; las tareas de dominio (TASK-218 a
TASK-226) solo escriben repositorios encima.

- Toda tabla lleva ``organization_id`` y, salvo la marca por organización, ``plant_id``; las de
  nodo referencian ``identity.node_identity (organization_id, plant_id, node_id)`` y las de zona
  ``identity.zone``, así que la base garantiza que nodo, zona, planta y organización concuerdan
  (BR-NUC-08). Las marcas de tiempo no tienen valor por defecto: las pone la aplicación con su
  ``Clock``.
- **Particiones mensuales** (UTC) de ``heartbeat_history`` (``received_at``),
  ``enrollment_attempt`` (``attempted_at``) y ``fleet_alarm`` (``raised_at``), con partición por
  defecto; esta migración crea el mes en curso y los tres siguientes y protege cada partición con
  ``shared.vigia_protect_append_only_partition`` (nuc_0002).
  ``shared.vigia_create_month_partitions`` y ``shared.vigia_default_partition_rows`` (nuc_0016) se
  **amplían** a las tres tablas: la tarea
  heredada ``create_partitions`` las mantiene y publica ``default_partition_rows`` sin cambiar.
- **Retención por archivado** (PAT-GOB-ESC-02, sin octava tarea): ``archive_audit_partitions``
  recorre también estas tablas (``shared.archive.table_archive``) con tres funciones
  ``SECURITY DEFINER`` de solo ``system`` análogas a las de la auditoría:
  ``shared.vigia_table_partition_summary``, ``shared.vigia_table_partition_rows`` (cada fila como
  el texto de ``to_jsonb``, en UTC y en orden de clave) y ``shared.vigia_detach_table_partition``
  (bloquea, comprueba de nuevo el recuento, que no hay filas posteriores ni alarmas abiertas, la
  desprende y la deja de solo anexar). Nada se borra dentro de una tabla viva.
- **Una sola alarma abierta por (clase, nodo)**: un índice único no cruza particiones, así que la
  garantía vive en la proyección no particionada ``fleet.open_fleet_alarm`` (clave primaria
  ``(organization_id, alarm_kind, node_id)``), que mantienen los disparadores de ``fleet_alarm``:
  abrir una alarma ocupa la ranura (``INSERT ... ON CONFLICT DO UPDATE ... WHERE alarm_id IS
  NULL``; si ya está ocupada, ``unique_violation``) y cerrarla la libera. ``vigia_app`` solo la lee.
  ``node_id`` es obligatorio en la alarma: las ocho clases de ``fleet_alarm_kind`` son de nodo y la
  garantía se expresa por nodo.
- La deduplicación de ``heartbeat_id`` no puede ser un índice único entre particiones: queda para
  TASK-223 sobre el índice ``(organization_id, plant_id, node_id, heartbeat_id)``.
- ``FORCE ROW LEVEL SECURITY`` con ``organization_isolation`` y la política RESTRICTIVE
  ``provider_concession_scope`` en todas las tablas con organización (sin planta, la de la marca
  por organización y los intentos de alta de un nodo desconocido: solo una concesión de toda la
  organización los alcanza). La marca **global** de publicación de la lista de revocación (D-7) es
  una fila única que solo ve el contexto de operador de ``regenerate_revocation_list``.
- Solo anexar (⛓, BR-NUC-43) con la guarda genérica ``catalog.guard_update()`` de gob_0017 y sus
  mismos argumentos: lista blanca de cierres (de nulo a valor una vez) y transiciones de estado
  solo hacia adelante. ``fleet_alarm`` se trata como ⛓ con cierre (``cleared_at``,
  ``cleared_event_id``): su retención es por archivado y nada se borra en la tabla viva. Las
  proyecciones con estado (``enrollment_code``, ``node_credential``, ``clip_upload_grant``) llevan
  la misma guarda. Disparadores con ``ENABLE ALWAYS``.
- ``vigia_app``: ``SELECT`` e ``INSERT``; ``UPDATE`` solo sobre la lista blanca de las ⛓ y las
  columnas que el diseño deja cambiar en las proyecciones 🔒; nunca ``DELETE`` ni ``TRUNCATE``.

Lo que no hace: lógica de identidad, códigos, credenciales, latido, alarmas y concesiones
(TASK-218 a TASK-226), registrar tareas (TASK-227) ni subir ``MINIMUM_SCHEMA_VERSION``.
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0018"
down_revision: str | None = "gob_0017"
branch_labels: None = None
depends_on: None = None

PARTITION_MONTHS_AHEAD = 3
"""Meses siguientes al actual que la migración deja creados (PAT-NUC-ESC-01)."""

_HEX64 = "'^[0-9a-f]{64}$'"
_SEMVER = (
    "'^(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)"
    "(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)([.](0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    "([+][0-9A-Za-z-]+([.][0-9A-Za-z-]+)*)?$'"
)
"""``SemVer`` del contrato (U-01): ``MAJOR.MINOR.PATCH`` con preliberación y metadatos."""

_FLEET_ALARM_KINDS = (
    "'node_mute', 'queue_over_threshold', 'clock_drift', 'version_retiring',"
    " 'simulated_adapter_in_productive', 'certificate_expiring', 'camera_below_min_fps',"
    " 'orphan_clips_growing'"
)
"""``fleet_alarm_kind`` (§4): ocho clases, todas de nodo."""

_FLEET_ALARM_KIND_ARRAY = f"ARRAY[{_FLEET_ALARM_KINDS}]::text[]"
_OBSERVABILITY = "'observable', 'degraded', 'not_observable'"
"""``observability_state`` del contrato (U-01)."""


def _semver(column: str, constraint: str) -> str:
    return (
        f"CONSTRAINT {constraint} CHECK (char_length({column}) BETWEEN 5 AND 64"
        f" AND {column} ~ {_SEMVER})"
    )


_FUNCTIONS = (
    # Número de claves de un objeto JSON (required_headers ≤ 8): un CHECK no admite subconsultas.
    """
    CREATE FUNCTION fleet.jsonb_object_size(document jsonb) RETURNS integer
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog
    AS $$
        SELECT CASE WHEN pg_catalog.jsonb_typeof(document) = 'object'
            THEN (SELECT count(*)::integer FROM pg_catalog.jsonb_object_keys(document))
        END
    $$
    """,
    # Una sola alarma abierta por (clase, nodo): abrir ocupa la ranura de open_fleet_alarm, que solo
    # se ocupa si está libre. SECURITY DEFINER (vigia_app solo lee la proyección); la seguridad a
    # nivel de fila sigue forzada para el dueño, así que la ranura es de la organización del
    # contexto.
    """
    CREATE FUNCTION fleet.open_alarm_slot() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        taken integer;
    BEGIN
        IF NEW.cleared_at IS NOT NULL THEN
            RETURN NULL;
        END IF;
        INSERT INTO fleet.open_fleet_alarm AS slot
            (organization_id, plant_id, alarm_kind, node_id, alarm_id, raised_at)
        VALUES
            (NEW.organization_id, NEW.plant_id, NEW.alarm_kind, NEW.node_id, NEW.alarm_id,
             NEW.raised_at)
        ON CONFLICT (organization_id, alarm_kind, node_id) DO UPDATE
            SET plant_id = EXCLUDED.plant_id,
                alarm_id = EXCLUDED.alarm_id,
                raised_at = EXCLUDED.raised_at
            WHERE slot.alarm_id IS NULL;
        GET DIAGNOSTICS taken = ROW_COUNT;
        IF taken <> 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'unique_violation',
                MESSAGE = format('ya hay una alarma %s abierta para el nodo %s',
                                 NEW.alarm_kind, NEW.node_id),
                CONSTRAINT = 'open_fleet_alarm_pkey';
        END IF;
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE FUNCTION fleet.release_alarm_slot() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF OLD.cleared_at IS NULL AND NEW.cleared_at IS NOT NULL THEN
            UPDATE fleet.open_fleet_alarm
                SET alarm_id = NULL, raised_at = NULL
                WHERE organization_id = NEW.organization_id
                    AND alarm_kind = NEW.alarm_kind
                    AND node_id = NEW.node_id
                    AND alarm_id = NEW.alarm_id;
        END IF;
        RETURN NULL;
    END
    $$
    """,
)

_TABLES = (
    # §3.1 NodeFleetRecord 🔒: espejo de NodeIdentity de U-02 con lo propio de la flota. El
    # reemplazo es un nodo nuevo (respuesta 15); la baja exige el nodo revocado (D-14).
    """
    CREATE TABLE fleet.node_fleet_record (
        node_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        replaces_node_id uuid REFERENCES fleet.node_fleet_record (node_id),
        hardware_fingerprint text
            CONSTRAINT node_fleet_record_fingerprint_format
                CHECK (hardware_fingerprint ~ '^[0-9a-f]{64}$'),
        declared_at timestamptz NOT NULL,
        declared_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        enrolled_at timestamptz,
        revoked_at timestamptz,
        revocation_reason_es text
            CONSTRAINT node_fleet_record_revocation_reason_length
                CHECK (char_length(revocation_reason_es) BETWEEN 10 AND 500),
        decommissioned_at timestamptz,
        live_view_local_url text
            CONSTRAINT node_fleet_record_live_view_local_url_format CHECK (
                char_length(live_view_local_url) <= 256
                AND live_view_local_url ~ '^https://[^/?#@[:space:]]+/$'
            ),
        CONSTRAINT node_fleet_record_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT node_fleet_record_not_self_replacing CHECK (replaces_node_id <> node_id),
        CONSTRAINT node_fleet_record_enrolled_after_declared CHECK (enrolled_at >= declared_at),
        CONSTRAINT node_fleet_record_revocation_complete
            CHECK ((revoked_at IS NULL) = (revocation_reason_es IS NULL)),
        CONSTRAINT node_fleet_record_decommission_requires_revocation CHECK (
            decommissioned_at IS NULL
            OR (revoked_at IS NOT NULL AND decommissioned_at >= revoked_at)
        )
    )
    """,
    "CREATE INDEX node_fleet_record_scope ON fleet.node_fleet_record (organization_id, plant_id)",
    # Nota de §3.1 (D-7): una sola lista de revocación global con marca única, que solo usa el
    # contexto de operador de regenerate_revocation_list. Fila única sembrada aquí.
    """
    CREATE TABLE fleet.revocation_list_publication (
        singleton boolean PRIMARY KEY DEFAULT true
            CONSTRAINT revocation_list_publication_singleton CHECK (singleton),
        crl_number bigint NOT NULL DEFAULT 0
            CONSTRAINT revocation_list_publication_crl_number CHECK (crl_number >= 0),
        published_at timestamptz,
        crl_sha256 text
            CONSTRAINT revocation_list_publication_crl_sha256_format
                CHECK (crl_sha256 ~ '^[0-9a-f]{64}$'),
        CONSTRAINT revocation_list_publication_complete
            CHECK ((published_at IS NULL) = (crl_sha256 IS NULL))
    )
    """,
    "INSERT INTO fleet.revocation_list_publication (singleton) VALUES (true)",
    # Nota de §3.1: la marca por organización queda solo como métrica.
    """
    CREATE TABLE fleet.revocation_list_dirty (
        organization_id uuid PRIMARY KEY REFERENCES identity.organization (organization_id),
        dirty boolean NOT NULL,
        updated_at timestamptz NOT NULL
    )
    """,
    # §3.2 EnrollmentCode 🔒: solo el hash con sal por código (respuesta 14, U03-H-13), válido
    # 24 h (BR-CTR-45) y un solo código activo por nodo.
    """
    CREATE TABLE fleet.enrollment_code (
        code_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        code_hash text NOT NULL
            CONSTRAINT enrollment_code_hash_format CHECK (code_hash ~ '^[0-9a-f]{64}$'),
        code_salt bytea NOT NULL
            CONSTRAINT enrollment_code_salt_length CHECK (octet_length(code_salt) = 16),
        issued_at timestamptz NOT NULL,
        issued_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        expires_at timestamptz NOT NULL,
        disclosed_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'active'
            CONSTRAINT enrollment_code_status_values
                CHECK (status IN ('active', 'used', 'expired', 'superseded')),
        ledger_record_id uuid NOT NULL,
        CONSTRAINT enrollment_code_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT enrollment_code_validity CHECK (expires_at = issued_at + interval '24 hours'),
        CONSTRAINT enrollment_code_disclosed_after_issue CHECK (disclosed_at >= issued_at)
    )
    """,
    """
    CREATE UNIQUE INDEX enrollment_code_one_active_per_node
        ON fleet.enrollment_code (node_id) WHERE status = 'active'
    """,
    """
    CREATE INDEX enrollment_code_scope
        ON fleet.enrollment_code (organization_id, plant_id, node_id, status)
    """,
    # §3.3 EnrollmentAttempt ⛓, particionada por mes de attempted_at. node_id nulo si el código no
    # corresponde a ningún nodo; entonces tampoco hay planta (nota del 2026-09-23: organización del
    # código o cubo global, lo decide TASK-218).
    f"""
    CREATE TABLE fleet.enrollment_attempt (
        attempt_id uuid NOT NULL,
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        plant_id uuid,
        node_id uuid,
        presented_code_hash text NOT NULL
            CONSTRAINT enrollment_attempt_code_hash_format
                CHECK (presented_code_hash ~ {_HEX64}),
        hardware_fingerprint text NOT NULL
            CONSTRAINT enrollment_attempt_fingerprint_format
                CHECK (hardware_fingerprint ~ {_HEX64}),
        software_version text NOT NULL
            {_semver("software_version", "enrollment_attempt_software_version")},
        contract_version text NOT NULL
            {_semver("contract_version", "enrollment_attempt_contract_version")},
        result text NOT NULL
            CONSTRAINT enrollment_attempt_result_values CHECK (result IN (
                'accepted', 'enrollment_code_used', 'enrollment_code_expired',
                'enrollment_code_invalid', 'rate_limited'
            )),
        attempted_at timestamptz NOT NULL,
        source_ip_hash text NOT NULL
            CONSTRAINT enrollment_attempt_source_ip_hash_format CHECK (source_ip_hash ~ {_HEX64}),
        correlation_id uuid NOT NULL,
        ledger_record_id uuid,
        CONSTRAINT enrollment_attempt_pkey PRIMARY KEY (attempt_id, attempted_at),
        CONSTRAINT enrollment_attempt_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT enrollment_attempt_plant_iff_node CHECK ((node_id IS NULL) = (plant_id IS NULL)),
        CONSTRAINT enrollment_attempt_accepted_has_node
            CHECK (result <> 'accepted' OR node_id IS NOT NULL)
    ) PARTITION BY RANGE (attempted_at)
    """,
    """
    CREATE INDEX enrollment_attempt_scope
        ON fleet.enrollment_attempt (organization_id, plant_id, node_id, attempted_at)
    """,
    # §3.4 NodeCredential 🔒: metadatos del certificado, nunca material secreto (BR-CTR-46). El
    # índice único (node_id, certificate_serial) incluye organización, planta y estado para la
    # consulta de identidad de cada petición (TASK-206) sin leer la tabla.
    """
    CREATE TABLE fleet.node_credential (
        credential_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        certificate_serial text NOT NULL
            CONSTRAINT node_credential_serial_format
                CHECK (certificate_serial ~ '^[0-9a-f]{1,64}$'),
        subject jsonb NOT NULL,
        key_algorithm text NOT NULL DEFAULT 'ecdsa_p256'
            CONSTRAINT node_credential_key_algorithm CHECK (key_algorithm = 'ecdsa_p256'),
        issued_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'active'
            CONSTRAINT node_credential_status_values
                CHECK (status IN ('active', 'overlapping', 'revoked', 'superseded')),
        rotated_from uuid REFERENCES fleet.node_credential (credential_id),
        revoked_at timestamptz,
        CONSTRAINT node_credential_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT node_credential_serial_unique UNIQUE (certificate_serial),
        CONSTRAINT node_credential_subject_matches CHECK (
            subject = jsonb_build_object(
                'node_id', node_id::text,
                'organization_id', organization_id::text,
                'plant_id', plant_id::text
            )
        ),
        CONSTRAINT node_credential_validity CHECK (expires_at > issued_at),
        CONSTRAINT node_credential_not_self_rotated CHECK (rotated_from <> credential_id),
        CONSTRAINT node_credential_revoked_has_date
            CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
        CONSTRAINT node_credential_revoked_after_issue CHECK (revoked_at >= issued_at)
    )
    """,
    """
    CREATE UNIQUE INDEX node_credential_identity
        ON fleet.node_credential (node_id, certificate_serial)
        INCLUDE (organization_id, plant_id, status)
    """,
    """
    CREATE INDEX node_credential_scope
        ON fleet.node_credential (organization_id, plant_id, node_id, status)
    """,
    # §3.5 NodeInventory 🔒: el estado vivo del nodo, una fila por nodo.
    f"""
    CREATE TABLE fleet.node_inventory (
        node_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        software_version text NOT NULL
            {_semver("software_version", "node_inventory_software_version")},
        contract_version text NOT NULL
            {_semver("contract_version", "node_inventory_contract_version")},
        model_version text NOT NULL
            CONSTRAINT node_inventory_model_version_length
                CHECK (char_length(model_version) BETWEEN 1 AND 64),
        contract_notice jsonb NOT NULL
            CONSTRAINT node_inventory_contract_notice_shape CHECK (
                jsonb_typeof(contract_notice) = 'object'
                AND jsonb_typeof(contract_notice -> 'result') = 'string'
            ),
        last_heartbeat_at timestamptz,
        communication_state text NOT NULL
            CONSTRAINT node_inventory_communication_state_values
                CHECK (communication_state IN ('unknown', 'reachable', 'mute')),
        local_queue jsonb NOT NULL
            CONSTRAINT node_inventory_local_queue_object
                CHECK (jsonb_typeof(local_queue) = 'object'),
        clock jsonb NOT NULL
            CONSTRAINT node_inventory_clock_object CHECK (jsonb_typeof(clock) = 'object'),
        signal_reader jsonb NOT NULL
            CONSTRAINT node_inventory_signal_reader_object
                CHECK (jsonb_typeof(signal_reader) = 'object'),
        uptime_seconds bigint NOT NULL
            CONSTRAINT node_inventory_uptime CHECK (uptime_seconds >= 0),
        target_version text
            {_semver("target_version", "node_inventory_target_version")},
        last_update_result text
            CONSTRAINT node_inventory_last_update_result_values
                CHECK (last_update_result IN ('applied', 'reverted', 'failed')),
        warnings text[] NOT NULL DEFAULT '{{}}'
            CONSTRAINT node_inventory_warnings CHECK (
                warnings <@ {_FLEET_ALARM_KIND_ARRAY}
                AND (cardinality(warnings) = 0 OR catalog.text_array_is_set(warnings))
            ),
        updated_at timestamptz NOT NULL,
        CONSTRAINT node_inventory_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id)
    )
    """,
    "CREATE INDEX node_inventory_scope ON fleet.node_inventory (organization_id, plant_id)",
    # §3.6 CameraInventory 🔒: una fila por (nodo, cámara). El tope de 1 a 8 cámaras lo fija el
    # latido del contrato: sin borrado, una cámara retirada conserva su fila y un recuento en la
    # base acabaría bloqueando al nodo.
    f"""
    CREATE TABLE fleet.camera_inventory (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        camera_id uuid NOT NULL,
        connected boolean NOT NULL,
        measured_fps double precision NOT NULL
            CONSTRAINT camera_inventory_measured_fps CHECK (measured_fps >= 0),
        declared_min_fps double precision NOT NULL
            CONSTRAINT camera_inventory_declared_min_fps CHECK (declared_min_fps BETWEEN 1 AND 60),
        observability_state text NOT NULL
            CONSTRAINT camera_inventory_observability_values
                CHECK (observability_state IN ({_OBSERVABILITY})),
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (node_id, camera_id),
        CONSTRAINT camera_inventory_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id)
    )
    """,
    """
    CREATE INDEX camera_inventory_scope
        ON fleet.camera_inventory (organization_id, plant_id, node_id)
    """,
    # §3.7 ZoneNodeState 🔒: una fila por (nodo, zona); el tope de 16 zonas, como el de cámaras.
    f"""
    CREATE TABLE fleet.zone_node_state (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        mode text NOT NULL
            CONSTRAINT zone_node_state_mode_values
                CHECK (mode IN ('no_capture', 'commissioning', 'productive')),
        observability_state text NOT NULL
            CONSTRAINT zone_node_state_observability_values
                CHECK (observability_state IN ({_OBSERVABILITY})),
        catalog_version_in_node integer NOT NULL
            CONSTRAINT zone_node_state_catalog_version CHECK (catalog_version_in_node >= 1),
        gate_state_valid_until timestamptz NOT NULL,
        open_episodes integer NOT NULL
            CONSTRAINT zone_node_state_open_episodes CHECK (open_episodes >= 0),
        coverage_ok boolean NOT NULL,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (node_id, zone_id),
        CONSTRAINT zone_node_state_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT zone_node_state_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id)
    )
    """,
    """
    CREATE INDEX zone_node_state_scope
        ON fleet.zone_node_state (organization_id, plant_id, zone_id, node_id)
    """,
    # §3.8 HeartbeatHistory ⛓, particionada por mes de received_at; 90 días en línea.
    """
    CREATE TABLE fleet.heartbeat_history (
        heartbeat_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        received_at timestamptz NOT NULL,
        sent_at timestamptz NOT NULL,
        payload_summary jsonb NOT NULL
            CONSTRAINT heartbeat_history_payload_summary_object
                CHECK (jsonb_typeof(payload_summary) = 'object'),
        CONSTRAINT heartbeat_history_pkey PRIMARY KEY (heartbeat_id, received_at),
        CONSTRAINT heartbeat_history_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id)
    ) PARTITION BY RANGE (received_at)
    """,
    """
    CREATE INDEX heartbeat_history_dedup
        ON fleet.heartbeat_history (organization_id, plant_id, node_id, heartbeat_id)
    """,
    """
    CREATE INDEX heartbeat_history_scope_received
        ON fleet.heartbeat_history (organization_id, plant_id, node_id, received_at)
    """,
    # §3.9 FleetAlarm ⛓ con cierre, particionada por mes de raised_at; 24 meses en línea.
    f"""
    CREATE TABLE fleet.fleet_alarm (
        alarm_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        alarm_kind text NOT NULL
            CONSTRAINT fleet_alarm_kind_values CHECK (alarm_kind IN ({_FLEET_ALARM_KINDS})),
        node_id uuid NOT NULL,
        zone_id uuid,
        raised_at timestamptz NOT NULL,
        cleared_at timestamptz,
        raised_event_id uuid NOT NULL,
        cleared_event_id uuid,
        CONSTRAINT fleet_alarm_pkey PRIMARY KEY (alarm_id, raised_at),
        CONSTRAINT fleet_alarm_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT fleet_alarm_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT fleet_alarm_clearance_complete
            CHECK ((cleared_at IS NULL) = (cleared_event_id IS NULL)),
        CONSTRAINT fleet_alarm_cleared_after_raised CHECK (cleared_at >= raised_at)
    ) PARTITION BY RANGE (raised_at)
    """,
    """
    CREATE INDEX fleet_alarm_scope
        ON fleet.fleet_alarm (organization_id, plant_id, node_id, alarm_kind, raised_at)
    """,
    # Proyección no particionada: la ranura de la alarma abierta de cada (clase, nodo).
    f"""
    CREATE TABLE fleet.open_fleet_alarm (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        alarm_kind text NOT NULL
            CONSTRAINT open_fleet_alarm_kind_values CHECK (alarm_kind IN ({_FLEET_ALARM_KINDS})),
        node_id uuid NOT NULL,
        alarm_id uuid,
        raised_at timestamptz,
        CONSTRAINT open_fleet_alarm_pkey PRIMARY KEY (organization_id, alarm_kind, node_id),
        CONSTRAINT open_fleet_alarm_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT open_fleet_alarm_slot_complete CHECK ((alarm_id IS NULL) = (raised_at IS NULL))
    )
    """,
    """
    CREATE INDEX open_fleet_alarm_scope
        ON fleet.open_fleet_alarm (organization_id, plant_id, node_id, alarm_kind)
    """,
    # §3.10 PlantFleetThresholds 🔒: una fila por planta, valores por defecto [estimación propia].
    """
    CREATE TABLE fleet.plant_fleet_thresholds (
        plant_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        queue_pending_threshold integer NOT NULL DEFAULT 100
            CONSTRAINT plant_fleet_thresholds_queue_pending CHECK (queue_pending_threshold >= 1),
        queue_age_threshold_minutes integer NOT NULL DEFAULT 30
            CONSTRAINT plant_fleet_thresholds_queue_age CHECK (queue_age_threshold_minutes >= 1),
        clock_drift_threshold_ms integer NOT NULL DEFAULT 5000
            CONSTRAINT plant_fleet_thresholds_clock_drift CHECK (clock_drift_threshold_ms >= 1),
        updated_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        updated_at timestamptz NOT NULL,
        CONSTRAINT plant_fleet_thresholds_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT plant_fleet_thresholds_scope UNIQUE (organization_id, plant_id)
    )
    """,
    # §3.11 TargetVersionPublication ⛓: ventana informativa en el piloto (D-5).
    f"""
    CREATE TABLE fleet.target_version_publication (
        publication_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        target_version text NOT NULL
            {_semver("target_version", "target_version_publication_target_version")},
        node_ids uuid[] NOT NULL
            CONSTRAINT target_version_publication_node_ids CHECK (
                cardinality(node_ids) BETWEEN 1 AND 1000
                AND array_ndims(node_ids) = 1
                AND array_position(node_ids, NULL) IS NULL
            ),
        maintenance_window_from timestamptz NOT NULL,
        maintenance_window_to timestamptz NOT NULL,
        published_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        published_at timestamptz NOT NULL,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT target_version_publication_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT target_version_publication_window
            CHECK (maintenance_window_to > maintenance_window_from)
    )
    """,
    """
    CREATE INDEX target_version_publication_scope
        ON fleet.target_version_publication (organization_id, plant_id, published_at)
    """,
    # §3.12 UpdateResult ⛓: el identificador nace en el nodo (clave de idempotencia).
    f"""
    CREATE TABLE fleet.update_result (
        update_result_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        target_version text NOT NULL
            {_semver("target_version", "update_result_target_version")},
        result text NOT NULL
            CONSTRAINT update_result_result_values
                CHECK (result IN ('applied', 'reverted', 'failed')),
        reported_at timestamptz NOT NULL,
        ledger_record_id uuid NOT NULL,
        CONSTRAINT update_result_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id)
    )
    """,
    """
    CREATE INDEX update_result_scope
        ON fleet.update_result (organization_id, plant_id, node_id, reported_at)
    """,
    # §3.13 ClipUploadGrant 🔒 (nota del 2026-09-23: purpose). Clave de BR-NUC-65; vencimiento de
    # 15 minutos como mucho; un clip de verificación nunca pasa a orphan (nota de BR-GOB-94).
    """
    CREATE TABLE fleet.clip_upload_grant (
        clip_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        purpose text NOT NULL DEFAULT 'evidence'
            CONSTRAINT clip_upload_grant_purpose_values
                CHECK (purpose IN ('evidence', 'verification')),
        storage_key text NOT NULL
            CONSTRAINT clip_upload_grant_storage_key_format CHECK (
                char_length(storage_key) <= 512
                AND storage_key ~ (
                    '^org/' || organization_id::text || '/plant/' || plant_id::text
                    || '/zone/' || zone_id::text || '/node/' || node_id::text || '/'
                    || clip_id::text || '[.][a-z0-9]{1,8}$'
                )
            ),
        content_type text NOT NULL
            CONSTRAINT clip_upload_grant_content_type_values
                CHECK (content_type IN ('video/mp4', 'image/jpeg')),
        max_size_bytes bigint NOT NULL
            CONSTRAINT clip_upload_grant_max_size CHECK (max_size_bytes BETWEEN 1 AND 52428800),
        required_headers jsonb NOT NULL
            CONSTRAINT clip_upload_grant_required_headers CHECK (
                fleet.jsonb_object_size(required_headers) BETWEEN 1 AND 8
                AND required_headers ? 'x-amz-checksum-sha256'
                AND required_headers ? 'x-amz-meta-vigia-anonymized'
            ),
        issued_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'issued'
            CONSTRAINT clip_upload_grant_status_values
                CHECK (status IN ('issued', 'used', 'expired', 'orphan')),
        used_at timestamptz,
        orphaned_at timestamptz,
        CONSTRAINT clip_upload_grant_storage_key_unique UNIQUE (storage_key),
        CONSTRAINT clip_upload_grant_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT clip_upload_grant_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT clip_upload_grant_expiry CHECK (
            expires_at > issued_at AND expires_at <= issued_at + interval '15 minutes'
        ),
        CONSTRAINT clip_upload_grant_used_has_date
            CHECK ((status IN ('used', 'orphan')) = (used_at IS NOT NULL)),
        CONSTRAINT clip_upload_grant_orphan_has_date
            CHECK ((status = 'orphan') = (orphaned_at IS NOT NULL)),
        CONSTRAINT clip_upload_grant_order CHECK (used_at >= issued_at AND orphaned_at >= used_at),
        CONSTRAINT clip_upload_grant_verification_never_orphan
            CHECK (purpose <> 'verification' OR status <> 'orphan')
    )
    """,
    """
    CREATE INDEX clip_upload_grant_scope
        ON fleet.clip_upload_grant (organization_id, plant_id, node_id, status, expires_at)
    """,
    # Nota de §3.13 VerificationClip ⛓: nace con la confirmación de un clip de verificación; la
    # comprobación automática del difuminado corre después (guarda de cierre del acta) y su
    # resultado es un cierre de nulo a valor.
    f"""
    CREATE TABLE fleet.verification_clip (
        clip_id uuid PRIMARY KEY REFERENCES fleet.clip_upload_grant (clip_id),
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        received_at timestamptz NOT NULL,
        sha256 text NOT NULL
            CONSTRAINT verification_clip_sha256_format CHECK (sha256 ~ {_HEX64}),
        blur_check_result jsonb
            CONSTRAINT verification_clip_blur_check_result_object
                CHECK (jsonb_typeof(blur_check_result) = 'object'),
        CONSTRAINT verification_clip_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT verification_clip_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id)
    )
    """,
    """
    CREATE INDEX verification_clip_scope
        ON fleet.verification_clip (organization_id, plant_id, zone_id, received_at)
    """,
    # Nota de §3.14 NodeConfiguration 🔒 (D-11, pendiente nº 35): valores por defecto
    # [objetivo propio]; mute_after_seconds = 5 por el intervalo.
    """
    CREATE TABLE fleet.node_configuration (
        node_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        time_sources jsonb NOT NULL
            CONSTRAINT node_configuration_time_sources CHECK (
                CASE WHEN jsonb_typeof(time_sources) = 'array'
                     THEN jsonb_array_length(time_sources) BETWEEN 1 AND 8 ELSE false END
            ),
        sent_records_retention_days integer NOT NULL DEFAULT 30
            CONSTRAINT node_configuration_retention CHECK (sent_records_retention_days >= 1),
        token_max_age_seconds integer NOT NULL DEFAULT 600
            CONSTRAINT node_configuration_token_max_age CHECK (token_max_age_seconds >= 1),
        heartbeat_interval_seconds integer NOT NULL DEFAULT 60
            CONSTRAINT node_configuration_heartbeat_interval
                CHECK (heartbeat_interval_seconds BETWEEN 15 AND 600),
        mute_after_seconds integer NOT NULL DEFAULT 300,
        grouping_window_ms integer NOT NULL DEFAULT 3000
            CONSTRAINT node_configuration_grouping_window CHECK (grouping_window_ms >= 0),
        updated_at timestamptz NOT NULL,
        CONSTRAINT node_configuration_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT node_configuration_mute_after
            CHECK (mute_after_seconds = 5 * heartbeat_interval_seconds)
    )
    """,
    "CREATE INDEX node_configuration_scope ON fleet.node_configuration (organization_id, plant_id)",
)

TABLES = (
    "node_fleet_record",
    "revocation_list_dirty",
    "enrollment_code",
    "enrollment_attempt",
    "node_credential",
    "node_inventory",
    "camera_inventory",
    "zone_node_state",
    "heartbeat_history",
    "fleet_alarm",
    "open_fleet_alarm",
    "plant_fleet_thresholds",
    "target_version_publication",
    "update_result",
    "clip_upload_grant",
    "verification_clip",
    "node_configuration",
)
"""Las tablas de ``fleet`` con organización: RLS forzada con las dos políticas. La marca global
``revocation_list_publication`` va aparte."""

GLOBAL_TABLE = "revocation_list_publication"

_ENTITIES = {
    "node_fleet_record": "NodeFleetRecord (domain-entities §3.1)",
    "revocation_list_dirty": "Marca revocation_list_dirty por organización, solo métrica (§3.1)",
    "enrollment_code": "EnrollmentCode (domain-entities §3.2)",
    "enrollment_attempt": "EnrollmentAttempt (domain-entities §3.3)",
    "node_credential": "NodeCredential (domain-entities §3.4)",
    "node_inventory": "NodeInventory (domain-entities §3.5)",
    "camera_inventory": "CameraInventory (domain-entities §3.6)",
    "zone_node_state": "ZoneNodeState (domain-entities §3.7)",
    "heartbeat_history": "HeartbeatHistory (domain-entities §3.8)",
    "fleet_alarm": "FleetAlarm (domain-entities §3.9)",
    "open_fleet_alarm": "Ranura de la alarma abierta por (clase, nodo) de FleetAlarm (§3.9)",
    "plant_fleet_thresholds": "PlantFleetThresholds (domain-entities §3.10)",
    "target_version_publication": "TargetVersionPublication (domain-entities §3.11)",
    "update_result": "UpdateResult (domain-entities §3.12)",
    "clip_upload_grant": "ClipUploadGrant (domain-entities §3.13)",
    "verification_clip": "VerificationClip (nota de domain-entities §3.13)",
    "node_configuration": "NodeConfiguration (nota de domain-entities §3.14)",
}

PARTITIONED_TABLES: dict[str, str] = {
    "heartbeat_history": "received_at",
    "enrollment_attempt": "attempted_at",
    "fleet_alarm": "raised_at",
}
"""Tablas particionadas por mes y su columna de partición."""

APPEND_ONLY_TABLES = (
    "enrollment_attempt",
    "heartbeat_history",
    "fleet_alarm",
    "target_version_publication",
    "update_result",
    "verification_clip",
)
"""Tablas ⛓ (``migrations/append_only.py``)."""

CLOSING_COLUMNS: dict[str, tuple[str, ...]] = {
    "fleet_alarm": ("cleared_at", "cleared_event_id"),
    "verification_clip": ("blur_check_result",),
    "node_credential": ("revoked_at",),
    "clip_upload_grant": ("used_at", "orphaned_at"),
}
"""Lista blanca de cierres: de nulo a su valor una sola vez. Las ⛓ que no aparecen no admiten
ningún ``UPDATE``."""

STATE_TRANSITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "enrollment_code": (
        "status",
        ("active>used", "active>expired", "active>superseded"),
    ),
    "node_credential": (
        "status",
        (
            "active>overlapping",
            "active>revoked",
            "active>superseded",
            "overlapping>revoked",
            "overlapping>superseded",
        ),
    ),
    "clip_upload_grant": ("status", ("issued>used", "issued>expired", "used>orphan")),
}
"""Columna de estado y transiciones admitidas (solo hacia adelante)."""

GUARDED_PROJECTIONS = ("enrollment_code", "node_credential", "clip_upload_grant")
"""Proyecciones 🔒 con la guarda de cierres y estados (disparador ``state_transition``)."""

_MUTABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "node_fleet_record": (
        "hardware_fingerprint",
        "enrolled_at",
        "revoked_at",
        "revocation_reason_es",
        "decommissioned_at",
        "live_view_local_url",
    ),
    "revocation_list_dirty": ("dirty", "updated_at"),
    "node_inventory": (
        "software_version",
        "contract_version",
        "model_version",
        "contract_notice",
        "last_heartbeat_at",
        "communication_state",
        "local_queue",
        "clock",
        "signal_reader",
        "uptime_seconds",
        "target_version",
        "last_update_result",
        "warnings",
        "updated_at",
    ),
    "camera_inventory": (
        "connected",
        "measured_fps",
        "declared_min_fps",
        "observability_state",
        "updated_at",
    ),
    "zone_node_state": (
        "mode",
        "observability_state",
        "catalog_version_in_node",
        "gate_state_valid_until",
        "open_episodes",
        "coverage_ok",
        "updated_at",
    ),
    "plant_fleet_thresholds": (
        "queue_pending_threshold",
        "queue_age_threshold_minutes",
        "clock_drift_threshold_ms",
        "updated_by",
        "updated_at",
    ),
    "node_configuration": (
        "time_sources",
        "sent_records_retention_days",
        "token_max_age_seconds",
        "heartbeat_interval_seconds",
        "mute_after_seconds",
        "grouping_window_ms",
        "updated_at",
    ),
    "open_fleet_alarm": (),
}
"""Proyecciones 🔒: columnas que el diseño deja cambiar (claves, organización y planta, nunca).
``open_fleet_alarm`` solo la escriben sus disparadores."""


def app_updatable_columns(table: str) -> tuple[str, ...]:
    """Columnas con ``UPDATE`` de ``vigia_app``: la lista blanca o las mutables de la tabla."""
    if table in _MUTABLE_COLUMNS:
        return _MUTABLE_COLUMNS[table]
    state = STATE_TRANSITIONS.get(table)
    return CLOSING_COLUMNS.get(table, ()) + ((state[0],) if state else ())


def _array(items: tuple[str, ...]) -> str:
    return "{" + ",".join(items) + "}"


def _update_guard(table: str) -> str:
    state, transitions = STATE_TRANSITIONS.get(table, ("", ()))
    arguments = (_array(CLOSING_COLUMNS.get(table, ())), state, _array(transitions), "{}")
    return "catalog.guard_update(" + ", ".join(f"'{argument}'" for argument in arguments) + ")"


_ORGANIZATION_POLICY = """
CREATE POLICY organization_isolation ON fleet.{table} AS PERMISSIVE FOR ALL TO PUBLIC
    USING (organization_id = shared.vigia_current_organization())
    WITH CHECK (organization_id = shared.vigia_current_organization())
"""

_PROVIDER_POLICY = """
CREATE POLICY provider_concession_scope ON fleet.{table} AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (identity.rls_provider_scope_allows(organization_id, {plant}))
    WITH CHECK (identity.rls_provider_scope_allows(organization_id, {plant}))
"""

_OPERATOR_POLICY = f"""
CREATE POLICY operator_only ON fleet.{GLOBAL_TABLE} AS PERMISSIVE FOR ALL TO PUBLIC
    USING (pg_catalog.current_setting('vigia.actor_kind', true) = 'operator')
    WITH CHECK (pg_catalog.current_setting('vigia.actor_kind', true) = 'operator')
"""


def provider_plant_column(table: str) -> str:
    """La planta que acota la concesión: nula en la marca por organización."""
    return "NULL" if table == "revocation_list_dirty" else "plant_id"


def _row_security() -> list[str]:
    statements: list[str] = []
    for table in TABLES:
        statements += [
            f"ALTER TABLE fleet.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE fleet.{table} FORCE ROW LEVEL SECURITY",
            _ORGANIZATION_POLICY.format(table=table),
            _PROVIDER_POLICY.format(table=table, plant=provider_plant_column(table)),
        ]
    statements += [
        f"ALTER TABLE fleet.{GLOBAL_TABLE} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE fleet.{GLOBAL_TABLE} FORCE ROW LEVEL SECURITY",
        _OPERATOR_POLICY,
    ]
    return statements


def _triggers() -> list[str]:
    statements: list[str] = []
    for table in APPEND_ONLY_TABLES:
        guarded = table in CLOSING_COLUMNS or table in STATE_TRANSITIONS
        update = _update_guard(table) if guarded else "shared.vigia_reject_mutation()"
        statements += [
            f"CREATE TRIGGER append_only_update BEFORE UPDATE ON fleet.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {update}",
            f"CREATE TRIGGER append_only_delete BEFORE DELETE ON fleet.{table}"
            " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
            f"CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON fleet.{table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()",
        ]
        for trigger in ("append_only_update", "append_only_delete", "append_only_no_truncate"):
            statements.append(f"ALTER TABLE fleet.{table} ENABLE ALWAYS TRIGGER {trigger}")
    for table in GUARDED_PROJECTIONS:
        statements += [
            f"CREATE TRIGGER state_transition BEFORE UPDATE ON fleet.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {_update_guard(table)}",
            f"ALTER TABLE fleet.{table} ENABLE ALWAYS TRIGGER state_transition",
        ]
    statements += [
        "CREATE TRIGGER open_alarm_slot AFTER INSERT ON fleet.fleet_alarm"
        " FOR EACH ROW EXECUTE FUNCTION fleet.open_alarm_slot()",
        "CREATE TRIGGER release_alarm_slot AFTER UPDATE OF cleared_at ON fleet.fleet_alarm"
        " FOR EACH ROW EXECUTE FUNCTION fleet.release_alarm_slot()",
        "ALTER TABLE fleet.fleet_alarm ENABLE ALWAYS TRIGGER open_alarm_slot",
        "ALTER TABLE fleet.fleet_alarm ENABLE ALWAYS TRIGGER release_alarm_slot",
    ]
    return statements


# Las particiones, después de los disparadores de la tabla padre: nacen con sus clones.
_MONTH_PARTITIONS = f"""
DO $$
DECLARE
    first_month CONSTANT timestamp := date_trunc('month', now() AT TIME ZONE 'UTC');
    month_start timestamp;
    parent text;
BEGIN
    FOREACH parent IN ARRAY ARRAY['heartbeat_history', 'enrollment_attempt', 'fleet_alarm'] LOOP
        EXECUTE format('CREATE TABLE fleet.%I PARTITION OF fleet.%I DEFAULT',
                       parent || '_default', parent);
        PERFORM shared.vigia_protect_append_only_partition(
            format('fleet.%I', parent || '_default')::regclass);
        FOR offset_months IN 0..{PARTITION_MONTHS_AHEAD} LOOP
            month_start := first_month + make_interval(months => offset_months);
            EXECUTE format(
                'CREATE TABLE fleet.%I PARTITION OF fleet.%I FOR VALUES FROM (%L) TO (%L)',
                parent || to_char(month_start, '"_"YYYY"_"MM'),
                parent,
                to_char(month_start, 'YYYY-MM-DD HH24:MI:SS') || '+00',
                to_char(month_start + interval '1 month', 'YYYY-MM-DD HH24:MI:SS') || '+00'
            );
            PERFORM shared.vigia_protect_append_only_partition(
                format('fleet.%I', parent || to_char(month_start, '"_"YYYY"_"MM'))::regclass);
        END LOOP;
    END LOOP;
END
$$
"""

# --- Mantenimiento y archivado (nuc_0016 ampliado) ---------------------------------------------

_ARCHIVABLE = "'fleet.heartbeat_history', 'fleet.enrollment_attempt', 'fleet.fleet_alarm'"

_PARTITION_FUNCTIONS = (
    # Igual que en nuc_0016, con las tres tablas de fleet en la lista.
    """
    CREATE OR REPLACE FUNCTION shared.vigia_create_month_partitions(
        first_month date, last_month date
    )
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
        -- Una llamada a la vez hasta el final de la transacción (la tarea semanal y la orden
        -- vigia-admin create-partitions comparten esta función).
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('vigia_create_month_partitions', 0));
        FOR target IN
            SELECT * FROM (VALUES
                ('ledger', 'ledger_record', 'received_at'),
                ('ledger', 'evidence', 'verified_at'),
                ('shared', 'audit_entry', 'occurred_at'),
                ('fleet', 'heartbeat_history', 'received_at'),
                ('fleet', 'enrollment_attempt', 'attempted_at'),
                ('fleet', 'fleet_alarm', 'raised_at')
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
                            PERFORM shared.vigia_protect_append_only_partition(
                                format('%I.%I', target.schema_name, partition_name)::regclass);
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
    COMMENT ON FUNCTION shared.vigia_create_month_partitions(date, date) IS
        'Particiones mensuales que falten de ledger_record, evidence, audit_entry y de las tres '
        'tablas de fleet, protegidas; salta el mes con filas en la partición por defecto '
        '(TASK-131, TASK-203)'
    """,
    """
    CREATE OR REPLACE FUNCTION shared.vigia_default_partition_rows()
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
            SELECT 'ledger.ledger_record', count(*) FROM ledger.ledger_record_default
            UNION ALL
            SELECT 'ledger.evidence', count(*) FROM ledger.evidence_default
            UNION ALL
            SELECT 'shared.audit_entry', count(*) FROM shared.audit_entry_default
            UNION ALL
            SELECT 'fleet.heartbeat_history', count(*) FROM fleet.heartbeat_history_default
            UNION ALL
            SELECT 'fleet.enrollment_attempt', count(*) FROM fleet.enrollment_attempt_default
            UNION ALL
            SELECT 'fleet.fleet_alarm', count(*) FROM fleet.fleet_alarm_default;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_default_partition_rows() IS
        'Filas de cada partición por defecto (default_partition_rows, PAT-NUC-ESC-01)'
    """,
    # Una partición adjunta de una tabla archivable, con el nombre del convenio, o un error.
    f"""
    CREATE FUNCTION shared.vigia_attached_table_partition(table_name text, partition_name text)
        RETURNS regclass
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        found regclass;
    BEGIN
        IF table_name IS NULL OR table_name NOT IN ({_ARCHIVABLE}) THEN
            RAISE EXCEPTION 'tabla no archivable' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF partition_name IS NULL OR partition_name !~ (
            '^' || pg_catalog.split_part(table_name, '.', 2) || '_[0-9]{{4}}_(0[1-9]|1[0-2])$'
        ) THEN
            RAISE EXCEPTION 'nombre de partición no válido'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        SELECT inheritance.inhrelid::regclass INTO found
            FROM pg_inherits AS inheritance
            JOIN pg_class AS child ON child.oid = inheritance.inhrelid
            WHERE inheritance.inhparent = table_name::regclass
                AND child.relname = partition_name;
        IF found IS NULL THEN
            RAISE EXCEPTION 'la partición no está adjunta' USING ERRCODE = 'undefined_table';
        END IF;
        RETURN found;
    END
    $$
    """,  # noqa: S608 - solo constantes del módulo, sin entrada externa
    "REVOKE ALL ON FUNCTION shared.vigia_attached_table_partition(text, text) FROM PUBLIC",
    # Columnas de clave (identificador e instante de partición) y predicado de fila abierta.
    """
    CREATE FUNCTION shared.vigia_table_partition_keys(
        table_name text, OUT id_column text, OUT at_column text, OUT open_predicate text
    )
        LANGUAGE sql
        IMMUTABLE
        SET search_path = pg_catalog
    AS $$
        SELECT keys.id_column, keys.at_column, keys.open_predicate
        FROM (VALUES
            ('fleet.heartbeat_history', 'heartbeat_id', 'received_at', 'false'),
            ('fleet.enrollment_attempt', 'attempt_id', 'attempted_at', 'false'),
            ('fleet.fleet_alarm', 'alarm_id', 'raised_at', 'cleared_at IS NULL')
        ) AS keys (table_name, id_column, at_column, open_predicate)
        WHERE keys.table_name = vigia_table_partition_keys.table_name
    $$
    """,
    "REVOKE ALL ON FUNCTION shared.vigia_table_partition_keys(text) FROM PUBLIC",
    """
    CREATE FUNCTION shared.vigia_table_partition_summary(table_name text, partition_name text)
        RETURNS TABLE (row_count bigint, open_rows bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        source regclass;
        keys record;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RAISE EXCEPTION 'solo el sistema lee una partición entera'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        source := shared.vigia_attached_table_partition(table_name, partition_name);
        keys := shared.vigia_table_partition_keys(table_name);
        RETURN QUERY EXECUTE format(
            'SELECT count(*), count(*) FILTER (WHERE %s) FROM %s', keys.open_predicate, source);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_table_partition_summary(text, text) IS
        'Filas y filas abiertas de una partición adjunta de una tabla archivable (TASK-203)'
    """,
    # Cada fila como el texto de to_jsonb, en UTC: la exportación compara byte a byte.
    """
    CREATE FUNCTION shared.vigia_table_partition_rows(
        table_name text, partition_name text, after_id uuid, after_at timestamptz,
        max_rows integer
    )
        RETURNS TABLE (row_id uuid, row_at timestamptz, row_json text)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET TimeZone = 'UTC'
    AS $$
    DECLARE
        source regclass;
        keys record;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RAISE EXCEPTION 'solo el sistema lee una partición entera'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        source := shared.vigia_attached_table_partition(table_name, partition_name);
        keys := shared.vigia_table_partition_keys(table_name);
        RETURN QUERY EXECUTE format(
            'SELECT %1$I, %2$I, to_jsonb(source_row)::text FROM %3$s AS source_row'
            ' WHERE (%1$I, %2$I) > ($1, $2) ORDER BY %1$I, %2$I LIMIT $3',
            keys.id_column, keys.at_column, source)
            USING coalesce(after_id, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(after_at, '-infinity'::timestamptz),
                greatest(least(max_rows, 10000), 1);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION
        shared.vigia_table_partition_rows(text, text, uuid, timestamptz, integer) IS
        'Lote de una partición adjunta de una tabla archivable, en orden de clave y como texto '
        'de to_jsonb en UTC (exportación del archivado, TASK-203)'
    """,
    """
    CREATE FUNCTION shared.vigia_detach_table_partition(
        table_name text, partition_name text, expected_rows bigint, not_after timestamptz
    )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET lock_timeout = '5s'
    AS $$
    DECLARE
        source regclass;
        keys record;
        found_rows bigint;
        open_rows bigint;
        newest timestamptz;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RAISE EXCEPTION 'solo el sistema desprende una partición'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        source := shared.vigia_attached_table_partition(table_name, partition_name);
        keys := shared.vigia_table_partition_keys(table_name);
        -- El candado espera a los escritores en curso y cierra el paso a los nuevos: el recuento
        -- de abajo es el de lo que se desprende. Tras esperarlo se comprueba de nuevo.
        EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', source);
        IF shared.vigia_attached_table_partition(table_name, partition_name)
            IS DISTINCT FROM source
        THEN
            RAISE EXCEPTION 'la partición no está adjunta' USING ERRCODE = 'undefined_table';
        END IF;
        EXECUTE format('SELECT count(*), count(*) FILTER (WHERE %s), max(%I) FROM %s',
                       keys.open_predicate, keys.at_column, source)
            INTO found_rows, open_rows, newest;
        IF found_rows IS DISTINCT FROM expected_rows
            OR open_rows <> 0
            OR not_after IS NULL
            OR (newest IS NOT NULL AND newest >= not_after)
        THEN
            RAISE EXCEPTION 'la partición no es la que se verificó'
                USING ERRCODE = 'object_not_in_prerequisite_state';
        END IF;
        EXECUTE format('ALTER TABLE %s DETACH PARTITION %s', table_name::regclass, source);
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = source AND tgname = 'archived_append_only_row'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER archived_append_only_row BEFORE UPDATE OR DELETE ON %s '
                'FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()', source);
        END IF;
        EXECUTE format('ALTER TABLE %s ENABLE ALWAYS TRIGGER archived_append_only_row', source);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION
        shared.vigia_detach_table_partition(text, text, bigint, timestamptz) IS
        'Desprende una partición de fleet ya archivada y verificada (sin filas posteriores ni '
        'alarmas abiertas); la tabla desprendida sigue siendo de solo anexar (TASK-203)'
    """,
)

_PARTITION_GRANTS = tuple(
    statement
    for signature in (
        "shared.vigia_table_partition_summary(text, text)",
        "shared.vigia_table_partition_rows(text, text, uuid, timestamptz, integer)",
        "shared.vigia_detach_table_partition(text, text, bigint, timestamptz)",
    )
    for statement in (
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {signature} TO vigia_app",
    )
)


def _grants() -> list[str]:
    statements = [
        "GRANT USAGE ON SCHEMA fleet TO vigia_app",
        "REVOKE ALL ON FUNCTION fleet.open_alarm_slot() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION fleet.release_alarm_slot() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION fleet.jsonb_object_size(jsonb) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION fleet.jsonb_object_size(jsonb) TO vigia_app",
        f"GRANT SELECT ON fleet.{GLOBAL_TABLE} TO vigia_app",
        f"GRANT UPDATE (crl_number, published_at, crl_sha256) ON fleet.{GLOBAL_TABLE} TO vigia_app",
    ]
    for table in TABLES:
        privileges = "SELECT" if table == "open_fleet_alarm" else "SELECT, INSERT"
        statements.append(f"GRANT {privileges} ON fleet.{table} TO vigia_app")
        columns = app_updatable_columns(table)
        if columns:
            statements.append(f"GRANT UPDATE ({', '.join(columns)}) ON fleet.{table} TO vigia_app")
    return statements


def _comments() -> list[str]:
    statements = [
        "COMMENT ON SCHEMA fleet IS"
        " 'Módulo fleet de U-03: identidad, credenciales, inventario, latidos, alarmas y"
        " concesiones de subida'",
        f"COMMENT ON TABLE fleet.{GLOBAL_TABLE} IS 'Marca única de publicación de la lista de"
        " revocación global (D-7); solo el contexto de operador'",
    ]
    for table in TABLES:
        mark = " ⛓ solo anexar" if table in APPEND_ONLY_TABLES else ""
        statements.append(
            f"COMMENT ON TABLE fleet.{table} IS '{_ENTITIES[table]}{mark}; seguridad a nivel de"
            " fila forzada por organización y concesión de proveedor'"
        )
    return statements


def upgrade() -> None:
    # Como en gob_0017: todo lo creado es de vigia_migrate, lo ejecute el maestro o él.
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute("CREATE SCHEMA fleet AUTHORIZATION vigia_migrate")
    for statement in (
        *_FUNCTIONS,
        *_TABLES,
        *_row_security(),
        *_triggers(),
        _MONTH_PARTITIONS,
        *_PARTITION_FUNCTIONS,
        *_PARTITION_GRANTS,
        *_grants(),
        *_comments(),
    ):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
