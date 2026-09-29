"""Esquema ``ledger``: expediente particionado, encadenado en la base y sobre canónico (TASK-108).

Revisión nuc_0002 (LC-NUC-12, parte 2). Lo que crea, todo de ``vigia_migrate``:

- **Seguridad a nivel de fila por organización** (domain-entities §6, PAT-NUC-SEG-01):
  ``shared.vigia_current_organization()`` lee ``vigia.organization_id`` (el ``SET LOCAL`` de
  ``shared.db``); sin contexto devuelve ``NULL`` y ninguna fila es visible ni insertable. Cada tabla
  de cliente lleva ``ENABLE`` y ``FORCE ROW LEVEL SECURITY`` con la política
  ``organization_isolation`` en la tabla padre.
- ``ledger.chain_head`` (``ChainHead``, §3.4): una fila por cadena (``kind`` ``ledger`` o
  ``audit``; ``plant_id`` nulo en las de organización). La única fila que se actualiza, y solo la
  actualiza el disparador: ``vigia_app`` solo la lee.
- ``ledger.ledger_record`` (``LedgerRecord``, §3.1), particionada por **rango mensual** de
  ``received_at`` (PAT-NUC-ESC-01): partición por defecto ``ledger.ledger_record_default`` y las
  del mes en curso y los tres siguientes (``ledger.ledger_record_AAAA_MM``, en UTC). ``content``
  son los bytes canónicos RFC 8785 (``bytea`` ≤ 256 KB) y ``content_json`` (``jsonb``) se genera
  de ellos. ``actor`` y ``scope`` se guardan en columnas para que el sobre tenga forma fija.
  ``occurred_at`` (opcional, fuera del sobre) es la marca del hecho que la unidad escritora extrae
  del contenido para la línea de tiempo (PAT-NUC-REN-04).
- ``ledger.record_source_key``: la unicidad de ``(organization_id, record_type, source_key)``
  (NFR-NUC-08). Un índice único de una tabla particionada tiene que incluir ``received_at``, así que
  la clave vive en esta tabla sin particionar, que llena el disparador.
- ``ledger.evidence`` (``Evidence``, §3.5), particionada por mes de ``verified_at``; ``anonymized``
  es la constante ``true`` (P3). Los campos de la verificación diferida de la marca los añade
  TASK-121 (adenda A-14).
- ``ledger.label`` (``Label``, §3.7) y ``ledger.communication_state`` (§3.8, proyección por nodo,
  la única de este esquema que la aplicación actualiza).
- Índices de NFR-NUC-08 y PAT-NUC-REN-04: ``(organization_id, plant_id, scope_zone_id,
  received_at)``, ``(organization_id, scope_zone_id, record_type, occurred_at)`` y el de la cadena
  ``(organization_id, plant_id, chain_sequence)`` para el verificador (TASK-118).
- ``ledger.vigia_canonical_envelope(ledger.ledger_record)``: los bytes RFC 8785 del sobre de
  BR-NUC-46 con **forma fija** (PAT-NUC-REN-01): claves en orden, cadenas con los escapes de
  ``to_json`` (los de ECMAScript), enteros sin exponente, marcas en UTC con milisegundos y ``Z``,
  ``null`` en ``plant_id`` ausente, y ``actor`` y ``scope`` con todas sus claves (``null`` en las
  ausentes). La equivalencia con ``rfc8785`` de Python la prueba el oráculo PR-NUC-47.
- ``ledger.vigia_chain_link()`` (``BEFORE INSERT``, ``SECURITY DEFINER`` con ``search_path``
  fijo): bloquea la ``ChainHead`` (la crea con secuencia 0 y el hash de génesis
  ``SHA-256("vigia:genesis:" + organization_id + ":" + (plant_id | "organization"))``), fija
  ``chain_sequence``, la marca (``received_at``, o ``occurred_at`` en auditoría), ``previous_hash``,
  ``content_hash = SHA-256(content)`` y ``record_hash = SHA-256(sobre ‖ previous_hash)``, con
  ``previous_hash`` como sus 64 caracteres hexadecimales en UTF-8, y actualiza la cabeza. Lo que la
  aplicación aporte en esas columnas se sobrescribe. La misma función encadena ``AuditEntry``
  (argumento ``audit``, nuc_0003).
- ``shared.vigia_reject_mutation()``: rechaza ``UPDATE``, ``DELETE`` y ``TRUNCATE`` en las tablas
  de solo anexar (BR-NUC-43) incluso para un superusuario; los disparadores se activan con
  ``ENABLE ALWAYS`` para que tampoco los salte ``session_replication_role = replica``.

**La marca de la cadena** se toma al obtener la exclusión (BR-NUC-47), con ``clock_timestamp()``
truncado a milisegundos y nunca menor que la de la cabeza: ``now()`` es la hora de inicio de la
transacción y dos escrituras concurrentes quedarían con marcas decrecientes. PostgreSQL enruta la
fila a su partición **antes** del disparador, con el valor por defecto de la columna; si la espera
de la exclusión cruza el cambio de mes, la partición ya no corresponde y el disparador responde
``serialization_failure`` (``40001``, transitorio: el escritor reintenta).

**Privilegios** (PAT-NUC-SEG-05): ``vigia_app`` tiene ``SELECT`` e ``INSERT`` en las tablas de
solo anexar, ``SELECT`` en ``chain_head`` y ``SELECT``, ``INSERT`` y ``UPDATE`` en
``communication_state``; ninguno sobre las particiones (se accede por la tabla padre, donde están
las políticas).
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0002"
down_revision: str | None = "nuc_0001"
branch_labels: None = None
depends_on: None = None

PARTITION_MONTHS_AHEAD = 3
"""Meses siguientes al actual con partición creada; los siguientes, ``create_partitions``."""

_ROLES = (
    "'coordinator_sst', 'line_manager', 'plant_manager', 'administrator', "
    "'provider_installer', 'copasst', 'platform_operator'"
)
_ACTOR_KINDS = "'user', 'provider_user', 'node', 'system', 'operator'"
_HEX64 = "'^[0-9a-f]{64}$'"
_SNAKE64 = "'^[a-z][a-z0-9_]{0,63}$'"

_HELPERS = (
    """
    CREATE FUNCTION shared.vigia_current_organization() RETURNS uuid
        LANGUAGE sql
        STABLE
        SET search_path = pg_catalog
    AS $$
        SELECT nullif(current_setting('vigia.organization_id', true), '')::uuid
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_current_organization() IS
        'Organización del ScopeContext (SET LOCAL vigia.organization_id); NULL sin contexto'
    """,
    """
    CREATE FUNCTION shared.vigia_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        RAISE EXCEPTION 'la tabla %.% es de solo anexar: % no está permitido',
            TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_reject_mutation() IS
        'Rechaza actualizar, borrar o vaciar una tabla de solo anexar (BR-NUC-43, PR-NUC-18)'
    """,
    # Los disparadores de fila de la tabla padre se clonan en cada partición; los de sentencia
    # (TRUNCATE) no. Cada partición de una tabla de solo anexar pasa por aquí al crearse: esta
    # migración, nuc_0003 y la tarea create_partitions (TASK-131).
    """
    CREATE FUNCTION shared.vigia_protect_append_only_partition(partition regclass) RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    DECLARE
        trigger_name name;
    BEGIN
        EXECUTE format(
            'CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON %s '
            'FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()', partition);
        FOR trigger_name IN
            SELECT tgname FROM pg_trigger WHERE tgrelid = partition AND NOT tgisinternal
        LOOP
            EXECUTE format('ALTER TABLE %s ENABLE ALWAYS TRIGGER %I', partition, trigger_name);
        END LOOP;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_protect_append_only_partition(regclass) IS
        'Protege una partición nueva de una tabla de solo anexar: vaciarla falla y todos sus '
        'disparadores quedan con ENABLE ALWAYS'
    """,
    "REVOKE ALL ON FUNCTION shared.vigia_protect_append_only_partition(regclass) FROM PUBLIC",
    # Bytes canónicos → jsonb. convert_from es STABLE por la codificación del servidor, que en
    # Vigía es siempre UTF8; la columna generada necesita una expresión IMMUTABLE.
    """
    CREATE FUNCTION ledger.vigia_bytes_to_jsonb(data bytea) RETURNS jsonb
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT convert_from(data, 'UTF8')::jsonb
    $$
    """,
    # Piezas del sobre (PAT-NUC-REN-01). to_json(text) escapa comillas, barra inversa, \b \f \n
    # \r \t y el resto de controles como \u00xx en minúsculas: lo mismo que RFC 8785 §3.2.2.2.
    """
    CREATE FUNCTION ledger.vigia_json_text(value text) RETURNS text
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT coalesce(to_json(value)::text, 'null')
    $$
    """,
    """
    CREATE FUNCTION ledger.vigia_json_uuid(value uuid) RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT coalesce('"' || value::text || '"', 'null')
    $$
    """,
    """
    CREATE FUNCTION ledger.vigia_json_integer(value bigint) RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT coalesce(value::text, 'null')
    $$
    """,
    """
    CREATE FUNCTION ledger.vigia_json_timestamp(value timestamptz) RETURNS text
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT coalesce(
            '"' || to_char(value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') || '"',
            'null'
        )
    $$
    """,
    """
    CREATE FUNCTION ledger.vigia_canonical_actor(
        kind text, id uuid, display_name_snapshot text, role_in_use text, concession_id uuid,
        unit text
    ) RETURNS text
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT '{"concession_id":' || ledger.vigia_json_uuid(concession_id)
            || ',"display_name_snapshot":' || ledger.vigia_json_text(display_name_snapshot)
            || ',"id":' || ledger.vigia_json_uuid(id)
            || ',"kind":' || ledger.vigia_json_text(kind)
            || ',"role_in_use":' || ledger.vigia_json_text(role_in_use)
            || ',"unit":' || ledger.vigia_json_text(unit)
            || '}'
    $$
    """,
    """
    COMMENT ON FUNCTION ledger.vigia_canonical_actor(text, uuid, text, text, uuid, text) IS
        'Actor del sobre canónico con sus seis claves fijas en orden RFC 8785; null si falta'
    """,
)

_CHAIN_HEAD = (
    f"""
    CREATE TABLE ledger.chain_head (
        organization_id uuid NOT NULL,
        plant_id uuid,
        kind text NOT NULL
            CONSTRAINT chain_head_kind CHECK (kind IN ('ledger', 'audit')),
        last_sequence bigint NOT NULL
            CONSTRAINT chain_head_last_sequence CHECK (last_sequence >= 0),
        last_hash text NOT NULL
            CONSTRAINT chain_head_last_hash CHECK (last_hash ~ {_HEX64}),
        updated_at timestamptz NOT NULL,
        CONSTRAINT chain_head_chain UNIQUE NULLS NOT DISTINCT (organization_id, kind, plant_id),
        CONSTRAINT chain_head_audit_organization CHECK (kind = 'ledger' OR plant_id IS NULL)
    )
    """,
    """
    COMMENT ON TABLE ledger.chain_head IS
        'Cabeza de cada cadena (ChainHead, domain-entities §3.4); solo la actualiza el disparador'
    """,
)

_LEDGER_RECORD = (
    f"""
    CREATE TABLE ledger.ledger_record (
        record_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid,
        chain_sequence bigint NOT NULL
            CONSTRAINT ledger_record_chain_sequence CHECK (chain_sequence >= 1),
        record_type text NOT NULL
            CONSTRAINT ledger_record_record_type REFERENCES ledger.record_type (record_type),
        schema_version integer NOT NULL
            CONSTRAINT ledger_record_schema_version CHECK (schema_version >= 1),
        actor_kind text NOT NULL
            CONSTRAINT ledger_record_actor_kind CHECK (actor_kind IN ({_ACTOR_KINDS})),
        actor_id uuid NOT NULL,
        actor_display_name_snapshot text NOT NULL
            CONSTRAINT ledger_record_actor_display_name
                CHECK (char_length(actor_display_name_snapshot) BETWEEN 1 AND 120),
        actor_role_in_use text
            CONSTRAINT ledger_record_actor_role CHECK (actor_role_in_use IN ({_ROLES})),
        actor_concession_id uuid,
        actor_unit text NOT NULL
            CONSTRAINT ledger_record_actor_unit CHECK (actor_unit IN ('U-02', 'U-03', 'U-04')),
        scope_plant_id uuid,
        scope_zone_id uuid,
        scope_node_id uuid,
        correlation_id uuid NOT NULL,
        received_at timestamptz NOT NULL DEFAULT date_trunc('milliseconds', clock_timestamp()),
        occurred_at timestamptz,
        source_key text
            CONSTRAINT ledger_record_source_key CHECK (char_length(source_key) BETWEEN 1 AND 64),
        content bytea NOT NULL
            CONSTRAINT ledger_record_content_size
                CHECK (octet_length(content) BETWEEN 1 AND 262144),
        content_json jsonb GENERATED ALWAYS AS (ledger.vigia_bytes_to_jsonb(content)) STORED,
        content_hash text NOT NULL
            CONSTRAINT ledger_record_content_hash CHECK (content_hash ~ {_HEX64}),
        previous_hash text NOT NULL
            CONSTRAINT ledger_record_previous_hash CHECK (previous_hash ~ {_HEX64}),
        record_hash text NOT NULL
            CONSTRAINT ledger_record_record_hash CHECK (record_hash ~ {_HEX64}),
        CONSTRAINT ledger_record_pkey PRIMARY KEY (record_id, received_at)
    ) PARTITION BY RANGE (received_at)
    """,
    """
    COMMENT ON TABLE ledger.ledger_record IS
        'Expediente (LedgerRecord, domain-entities §3.1): solo anexar, particionado por mes'
    """,
    """
    CREATE INDEX ledger_record_zone_received
        ON ledger.ledger_record (organization_id, plant_id, scope_zone_id, received_at)
    """,
    """
    CREATE INDEX ledger_record_zone_type_occurred
        ON ledger.ledger_record (organization_id, scope_zone_id, record_type, occurred_at)
        WHERE occurred_at IS NOT NULL
    """,
    """
    CREATE INDEX ledger_record_chain
        ON ledger.ledger_record (organization_id, plant_id, chain_sequence)
    """,
    f"""
    CREATE TABLE ledger.record_source_key (
        organization_id uuid NOT NULL,
        record_type text NOT NULL,
        source_key text NOT NULL
            CONSTRAINT record_source_key_length CHECK (char_length(source_key) BETWEEN 1 AND 64),
        record_id uuid NOT NULL,
        received_at timestamptz NOT NULL,
        record_hash text NOT NULL
            CONSTRAINT record_source_key_record_hash CHECK (record_hash ~ {_HEX64}),
        CONSTRAINT record_source_key_pkey PRIMARY KEY (organization_id, record_type, source_key)
    )
    """,
    """
    COMMENT ON TABLE ledger.record_source_key IS
        'Clave de idempotencia única por organización y tipo (BR-CTR-26, NFR-NUC-08); la llena '
        'el disparador de encadenado'
    """,
)

_EVIDENCE = (
    f"""
    CREATE TABLE ledger.evidence (
        evidence_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        record_id uuid NOT NULL,
        clip_id uuid NOT NULL,
        camera_id uuid NOT NULL,
        storage_key text NOT NULL
            CONSTRAINT evidence_storage_key CHECK (char_length(storage_key) BETWEEN 1 AND 512),
        sha256 text NOT NULL
            CONSTRAINT evidence_sha256 CHECK (sha256 ~ {_HEX64}),
        size_bytes bigint NOT NULL
            CONSTRAINT evidence_size_bytes CHECK (size_bytes >= 1),
        content_type text NOT NULL
            CONSTRAINT evidence_content_type CHECK (char_length(content_type) BETWEEN 1 AND 128),
        media_kind text NOT NULL
            CONSTRAINT evidence_media_kind CHECK (media_kind IN ('video', 'image')),
        duration_ms integer
            CONSTRAINT evidence_duration_ms CHECK (duration_ms >= 0),
        segment text NOT NULL
            CONSTRAINT evidence_segment CHECK (segment IN ('full', 'start', 'end')),
        starts_at timestamptz,
        ends_at timestamptz,
        anonymized boolean NOT NULL DEFAULT true
            CONSTRAINT evidence_anonymized CHECK (anonymized),
        verification_method text NOT NULL DEFAULT 'object_metadata'
            CONSTRAINT evidence_verification_method
                CHECK (verification_method IN ('object_metadata', 'full_read')),
        verified_at timestamptz NOT NULL,
        CONSTRAINT evidence_pkey PRIMARY KEY (evidence_id, verified_at),
        CONSTRAINT evidence_interval CHECK (ends_at >= starts_at)
    ) PARTITION BY RANGE (verified_at)
    """,
    """
    COMMENT ON TABLE ledger.evidence IS
        'Clips difuminados custodiados (Evidence, domain-entities §3.5): solo anexar, por mes'
    """,
    """
    CREATE INDEX evidence_zone_verified
        ON ledger.evidence (organization_id, plant_id, zone_id, verified_at)
    """,
    "CREATE INDEX evidence_record ON ledger.evidence (organization_id, record_id)",
)

_LABEL = (
    f"""
    CREATE TABLE ledger.label (
        label_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        source_record_id uuid NOT NULL,
        subject_record_id uuid NOT NULL,
        family text NOT NULL
            CONSTRAINT label_family CHECK (family ~ {_SNAKE64}),
        outcome text NOT NULL
            CONSTRAINT label_outcome CHECK (outcome IN ('confirmed', 'authorized_operation',
                'false_positive', 'review_confirmed', 'review_discarded')),
        reason_category text NOT NULL
            CONSTRAINT label_reason_category CHECK (reason_category ~ {_SNAKE64}),
        evidence_ids uuid[] NOT NULL DEFAULT '{{}}',
        labeled_at timestamptz NOT NULL,
        labeled_by jsonb NOT NULL
            CONSTRAINT label_labeled_by CHECK (jsonb_typeof(labeled_by) = 'object')
    )
    """,
    """
    COMMENT ON TABLE ledger.label IS
        'Decisión humana como dato (Label, domain-entities §3.7): proyección de solo anexar'
    """,
    """
    CREATE INDEX label_zone_family_reason
        ON ledger.label (organization_id, zone_id, family, reason_category)
    """,
    "CREATE INDEX label_subject ON ledger.label (organization_id, subject_record_id)",
)

_COMMUNICATION_STATE = (
    """
    CREATE TABLE ledger.communication_state (
        node_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        state text NOT NULL
            CONSTRAINT communication_state_state CHECK (state IN ('unknown', 'reachable', 'mute')),
        since timestamptz NOT NULL,
        last_heartbeat_at timestamptz,
        source_record_id uuid NOT NULL
    )
    """,
    """
    COMMENT ON TABLE ledger.communication_state IS
        'Estado de comunicación por nodo (CommunicationState, domain-entities §3.8): proyección'
    """,
    """
    CREATE INDEX communication_state_plant
        ON ledger.communication_state (organization_id, plant_id)
    """,
)

# Mes en curso y los tres siguientes, en UTC, para las dos tablas particionadas del esquema.
_MONTH_PARTITIONS = f"""
DO $$
DECLARE
    first_month CONSTANT timestamp := date_trunc('month', now() AT TIME ZONE 'UTC');
    parent text;
    month_start timestamp;
BEGIN
    CREATE TABLE ledger.ledger_record_default PARTITION OF ledger.ledger_record DEFAULT;
    CREATE TABLE ledger.evidence_default PARTITION OF ledger.evidence DEFAULT;
    FOREACH parent IN ARRAY ARRAY['ledger_record', 'evidence'] LOOP
        FOR offset_months IN 0..{PARTITION_MONTHS_AHEAD} LOOP
            month_start := first_month + make_interval(months => offset_months);
            EXECUTE format(
                'CREATE TABLE ledger.%I PARTITION OF ledger.%I FOR VALUES FROM (%L) TO (%L)',
                parent || to_char(month_start, '"_"YYYY"_"MM'),
                parent,
                to_char(month_start, 'YYYY-MM-DD HH24:MI:SS') || '+00',
                to_char(month_start + interval '1 month', 'YYYY-MM-DD HH24:MI:SS') || '+00'
            );
            PERFORM shared.vigia_protect_append_only_partition(
                format('ledger.%I', parent || to_char(month_start, '"_"YYYY"_"MM'))::regclass);
        END LOOP;
    END LOOP;
    PERFORM shared.vigia_protect_append_only_partition('ledger.ledger_record_default');
    PERFORM shared.vigia_protect_append_only_partition('ledger.evidence_default');
END
$$
"""

_CANONICAL_ENVELOPE = (
    """
    CREATE FUNCTION ledger.vigia_canonical_envelope(record ledger.ledger_record) RETURNS bytea
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT convert_to(
            '{"actor":' || ledger.vigia_canonical_actor(
                    record.actor_kind, record.actor_id, record.actor_display_name_snapshot,
                    record.actor_role_in_use, record.actor_concession_id, record.actor_unit)
                || ',"chain_sequence":' || ledger.vigia_json_integer(record.chain_sequence)
                || ',"content_hash":' || ledger.vigia_json_text(record.content_hash)
                || ',"correlation_id":' || ledger.vigia_json_uuid(record.correlation_id)
                || ',"organization_id":' || ledger.vigia_json_uuid(record.organization_id)
                || ',"plant_id":' || ledger.vigia_json_uuid(record.plant_id)
                || ',"received_at":' || ledger.vigia_json_timestamp(record.received_at)
                || ',"record_id":' || ledger.vigia_json_uuid(record.record_id)
                || ',"record_type":' || ledger.vigia_json_text(record.record_type)
                || ',"schema_version":' || ledger.vigia_json_integer(record.schema_version)
                || ',"scope":{"node_id":' || ledger.vigia_json_uuid(record.scope_node_id)
                || ',"plant_id":' || ledger.vigia_json_uuid(record.scope_plant_id)
                || ',"zone_id":' || ledger.vigia_json_uuid(record.scope_zone_id)
                || '}}',
            'UTF8'
        )
    $$
    """,
    """
    COMMENT ON FUNCTION ledger.vigia_canonical_envelope(ledger.ledger_record) IS
        'Bytes RFC 8785 del sobre de BR-NUC-46 con forma fija (PAT-NUC-REN-01, PR-NUC-47)'
    """,
)

# SECURITY DEFINER: bloquea y actualiza chain_head, que vigia_app solo puede leer. Corre como
# vigia_migrate, sujeto también a la seguridad a nivel de fila (FORCE) con el contexto de la
# sesión: la fila debe ser de la organización del contexto.
_CHAIN_LINK = (
    """
    CREATE FUNCTION ledger.vigia_chain_link() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        chain_kind CONSTANT text := TG_ARGV[0];
        routed_at timestamptz;
        chain_plant uuid;
        expected_level text;
        head ledger.chain_head;
        taken_at timestamptz;
    BEGIN
        IF chain_kind NOT IN ('ledger', 'audit') THEN
            RAISE EXCEPTION 'vigia_chain_link: tipo de cadena desconocido %', chain_kind;
        END IF;
        IF NEW.organization_id IS DISTINCT FROM shared.vigia_current_organization() THEN
            RAISE EXCEPTION 'la fila no es de la organización del contexto de alcance'
                USING ERRCODE = 'insufficient_privilege';
        END IF;

        IF chain_kind = 'ledger' THEN
            SELECT chain_level INTO expected_level
            FROM ledger.record_type WHERE record_type = NEW.record_type;
            IF (expected_level = 'plant') <> (NEW.plant_id IS NOT NULL) THEN
                RAISE EXCEPTION 'el tipo % es de cadena % y plant_id no concuerda',
                    NEW.record_type, expected_level
                    USING ERRCODE = 'check_violation';
            END IF;
            chain_plant := NEW.plant_id;
            routed_at := NEW.received_at;
        ELSE
            chain_plant := NULL;
            routed_at := NEW.occurred_at;
        END IF;

        -- Exclusión sobre la cabeza (BR-NUC-47); si no existe, se crea con el hash de génesis.
        SELECT * INTO head FROM ledger.chain_head
        WHERE organization_id = NEW.organization_id AND kind = chain_kind
          AND plant_id IS NOT DISTINCT FROM chain_plant
        FOR UPDATE;
        IF NOT FOUND THEN
            INSERT INTO ledger.chain_head
                (organization_id, plant_id, kind, last_sequence, last_hash, updated_at)
            VALUES (
                NEW.organization_id, chain_plant, chain_kind, 0,
                encode(public.digest(convert_to(
                    'vigia:genesis:' || NEW.organization_id::text || ':'
                        || coalesce(chain_plant::text, 'organization'),
                    'UTF8'), 'sha256'), 'hex'),
                '-infinity'
            )
            ON CONFLICT ON CONSTRAINT chain_head_chain DO NOTHING;
            SELECT * INTO STRICT head FROM ledger.chain_head
            WHERE organization_id = NEW.organization_id AND kind = chain_kind
              AND plant_id IS NOT DISTINCT FROM chain_plant
            FOR UPDATE;
        END IF;

        -- La marca se toma con la exclusión ya obtenida y nunca retrocede en la cadena.
        taken_at := greatest(date_trunc('milliseconds', clock_timestamp()), head.updated_at);
        IF routed_at IS NULL
           OR date_trunc('month', routed_at AT TIME ZONE 'UTC')
              <> date_trunc('month', taken_at AT TIME ZONE 'UTC') THEN
            RAISE EXCEPTION 'la marca de la cadena cambió de mes mientras se esperaba la '
                'exclusión; reintenta la escritura'
                USING ERRCODE = 'serialization_failure';
        END IF;

        NEW.chain_sequence := head.last_sequence + 1;
        NEW.previous_hash := head.last_hash;
        IF chain_kind = 'ledger' THEN
            NEW.received_at := taken_at;
            NEW.content_hash := encode(public.digest(NEW.content, 'sha256'), 'hex');
            NEW.record_hash := encode(public.digest(
                ledger.vigia_canonical_envelope(NEW) || convert_to(NEW.previous_hash, 'UTF8'),
                'sha256'), 'hex');
            UPDATE ledger.chain_head
            SET last_sequence = NEW.chain_sequence, last_hash = NEW.record_hash,
                updated_at = taken_at
            WHERE organization_id = NEW.organization_id AND kind = chain_kind
              AND plant_id IS NOT DISTINCT FROM chain_plant;
            IF NEW.source_key IS NOT NULL THEN
                INSERT INTO ledger.record_source_key
                    (organization_id, record_type, source_key, record_id, received_at,
                     record_hash)
                VALUES (NEW.organization_id, NEW.record_type, NEW.source_key, NEW.record_id,
                        NEW.received_at, NEW.record_hash);
            END IF;
        ELSE
            NEW.occurred_at := taken_at;
            NEW.filters_hash := encode(public.digest(NEW.filters, 'sha256'), 'hex');
            NEW.entry_hash := encode(public.digest(
                shared.vigia_canonical_audit_envelope(NEW)
                    || convert_to(NEW.previous_hash, 'UTF8'),
                'sha256'), 'hex');
            UPDATE ledger.chain_head
            SET last_sequence = NEW.chain_sequence, last_hash = NEW.entry_hash,
                updated_at = taken_at
            WHERE organization_id = NEW.organization_id AND kind = chain_kind
              AND plant_id IS NULL;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION ledger.vigia_chain_link() IS
        'Encadenado en la base (BR-NUC-46, 47, 60): exclusión, secuencia, marca y hashes'
    """,
    "REVOKE ALL ON FUNCTION ledger.vigia_chain_link() FROM PUBLIC",
    """
    CREATE TRIGGER ledger_record_chain_link
        BEFORE INSERT ON ledger.ledger_record
        FOR EACH ROW EXECUTE FUNCTION ledger.vigia_chain_link('ledger')
    """,
    "ALTER TABLE ledger.ledger_record ENABLE ALWAYS TRIGGER ledger_record_chain_link",
)

_CLIENT_TABLES = (
    "ledger.chain_head",
    "ledger.ledger_record",
    "ledger.record_source_key",
    "ledger.evidence",
    "ledger.label",
    "ledger.communication_state",
)
_APPEND_ONLY_TABLES = (
    "ledger.ledger_record",
    "ledger.record_source_key",
    "ledger.evidence",
    "ledger.label",
)


def _row_security(table: str) -> tuple[str, ...]:
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY organization_isolation ON {table}
            USING (organization_id = shared.vigia_current_organization())
            WITH CHECK (organization_id = shared.vigia_current_organization())
        """,
    )


def _append_only(table: str) -> tuple[str, ...]:
    name = table.split(".")[1]
    return (
        f"""
        CREATE TRIGGER {name}_append_only_row
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()
        """,
        f"""
        CREATE TRIGGER {name}_append_only_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()
        """,
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}_append_only_row",
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}_append_only_truncate",
    )


_APP_GRANTS = (
    "GRANT EXECUTE ON FUNCTION shared.vigia_current_organization() TO vigia_app",
    "REVOKE ALL ON FUNCTION shared.vigia_reject_mutation() FROM PUBLIC",
    "GRANT SELECT ON ledger.chain_head TO vigia_app",
    "GRANT SELECT, INSERT ON ledger.ledger_record TO vigia_app",
    "GRANT SELECT ON ledger.record_source_key TO vigia_app",
    "GRANT SELECT, INSERT ON ledger.evidence TO vigia_app",
    "GRANT SELECT, INSERT ON ledger.label TO vigia_app",
    "GRANT SELECT, INSERT, UPDATE ON ledger.communication_state TO vigia_app",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (
        *_HELPERS,
        *_CHAIN_HEAD,
        *_LEDGER_RECORD,
        *_EVIDENCE,
        *_LABEL,
        *_COMMUNICATION_STATE,
        *_CANONICAL_ENVELOPE,
        *_CHAIN_LINK,
    ):
        op.execute(statement)
    for table in _CLIENT_TABLES:
        for statement in _row_security(table):
            op.execute(statement)
    for table in _APPEND_ONLY_TABLES:
        for statement in _append_only(table):
            op.execute(statement)
    # Las particiones, después de los disparadores de la tabla padre: nacen con sus clones.
    op.execute(_MONTH_PARTITIONS)
    for statement in _APP_GRANTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
