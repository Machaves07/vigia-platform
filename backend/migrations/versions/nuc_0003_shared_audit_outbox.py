"""Esquema ``shared``: auditoría encadenada, bandeja de salida y cola muerta (TASK-108).

Revisión nuc_0003 (LC-NUC-12, parte 3). Usa lo que creó nuc_0002 (seguridad a nivel de fila por
organización, ``ledger.chain_head``, ``ledger.vigia_chain_link()`` y
``shared.vigia_reject_mutation()``). Todo de ``vigia_migrate``:

- ``shared.audit_entry`` (``AuditEntry``, domain-entities §4.2), particionada por **rango mensual**
  de ``occurred_at`` con partición por defecto y las del mes en curso y los tres siguientes, en
  UTC. Cadena por organización (``chain_head.kind = 'audit'``, ``plant_id`` nulo) con las mismas
  reglas que el expediente (BR-NUC-60): la encadena ``ledger.vigia_chain_link('audit')``, que fija
  ``chain_sequence``, ``occurred_at`` (al obtener la exclusión, no decreciente), ``previous_hash``,
  ``filters_hash = SHA-256(filters)`` y ``entry_hash = SHA-256(sobre ‖ previous_hash)``.
  ``filters`` son los bytes canónicos RFC 8785 de los filtros (≤ 4 KB) y ``filters_json`` se genera
  de ellos, igual que ``content`` en el expediente: el sobre lleva su hash, no el documento.
- ``shared.audit_entry_identity``: ``entry_id`` único en toda la auditoría (la clave primaria de la
  tabla particionada incluye ``occurred_at``). El disparador lo reclama sin ``ON CONFLICT`` antes
  de avanzar la cabeza, igual que ``ledger.record_identity``; ``vigia_app`` no tiene privilegios
  sobre ella.
- ``shared.vigia_canonical_audit_envelope(shared.audit_entry)``: bytes RFC 8785 del sobre de la
  entrada con forma fija, claves ``actor``, ``chain_sequence``, ``correlation_id``, ``entry_id``,
  ``filters_hash``, ``occurred_at``, ``operation``, ``organization_id``, ``outcome``,
  ``resource_ref`` (``{"id", "kind"}`` o ``null``), ``result_count`` y ``scope``
  (``{"plant_id", "zone_id"}``). Probada por el oráculo PR-NUC-47.
- ``shared.outbox_event`` (``OutboxEvent``, §4.3), de solo anexar; ``partition_key`` se genera como
  ``organization_id:plant_id`` o ``organization_id:organization``.
- ``shared.outbox_delivery`` (``OutboxDelivery``), una fila por evento y consumidor; la aplicación
  la actualiza al entregar o reintentar.
- ``shared.dead_letter`` (``DeadLetter``), de solo anexar.

Todas con ``ENABLE`` y ``FORCE ROW LEVEL SECURITY`` y la política ``organization_isolation``.
``vigia_app`` tiene ``SELECT`` e ``INSERT`` en las de solo anexar y además ``UPDATE`` en
``outbox_delivery``; ninguno sobre las particiones.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0003"
down_revision: str | None = "nuc_0002"
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

_AUDIT_ENTRY = (
    f"""
    CREATE TABLE shared.audit_entry (
        entry_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        chain_sequence bigint NOT NULL
            CONSTRAINT audit_entry_chain_sequence CHECK (chain_sequence >= 1),
        actor_kind text NOT NULL
            CONSTRAINT audit_entry_actor_kind CHECK (actor_kind IN ({_ACTOR_KINDS})),
        actor_id uuid NOT NULL,
        actor_display_name_snapshot text NOT NULL
            CONSTRAINT audit_entry_actor_display_name
                CHECK (char_length(actor_display_name_snapshot) BETWEEN 1 AND 120),
        actor_role_in_use text
            CONSTRAINT audit_entry_actor_role CHECK (actor_role_in_use IN ({_ROLES})),
        actor_concession_id uuid,
        actor_unit text NOT NULL
            CONSTRAINT audit_entry_actor_unit CHECK (actor_unit IN ('U-02', 'U-03', 'U-04')),
        operation text NOT NULL
            CONSTRAINT audit_entry_operation CHECK (operation ~ {_SNAKE64}),
        scope_plant_id uuid,
        scope_zone_id uuid,
        resource_kind text
            CONSTRAINT audit_entry_resource_kind CHECK (resource_kind ~ {_SNAKE64}),
        resource_id uuid,
        filters bytea
            CONSTRAINT audit_entry_filters_size CHECK (octet_length(filters) BETWEEN 1 AND 4096),
        filters_json jsonb GENERATED ALWAYS AS (ledger.vigia_bytes_to_jsonb(filters)) STORED,
        filters_hash text
            CONSTRAINT audit_entry_filters_hash CHECK (filters_hash ~ {_HEX64}),
        result_count integer
            CONSTRAINT audit_entry_result_count CHECK (result_count >= 0),
        outcome text NOT NULL
            CONSTRAINT audit_entry_outcome CHECK (outcome IN ('success', 'denied', 'error')),
        correlation_id uuid NOT NULL,
        occurred_at timestamptz NOT NULL DEFAULT date_trunc('milliseconds', clock_timestamp()),
        previous_hash text NOT NULL
            CONSTRAINT audit_entry_previous_hash CHECK (previous_hash ~ {_HEX64}),
        entry_hash text NOT NULL
            CONSTRAINT audit_entry_entry_hash CHECK (entry_hash ~ {_HEX64}),
        CONSTRAINT audit_entry_pkey PRIMARY KEY (entry_id, occurred_at),
        CONSTRAINT audit_entry_resource_ref
            CHECK ((resource_kind IS NULL) = (resource_id IS NULL))
    ) PARTITION BY RANGE (occurred_at)
    """,
    """
    COMMENT ON TABLE shared.audit_entry IS
        'Registro de auditoría (AuditEntry, domain-entities §4.2): solo anexar, por mes'
    """,
    "CREATE INDEX audit_entry_chain ON shared.audit_entry (organization_id, chain_sequence)",
    """
    CREATE TABLE shared.audit_entry_identity (
        entry_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        occurred_at timestamptz NOT NULL
    )
    """,
    """
    COMMENT ON TABLE shared.audit_entry_identity IS
        'entry_id único en toda la auditoría; lo reclama el disparador de encadenado antes de '
        'avanzar la cabeza'
    """,
    """
    CREATE INDEX audit_entry_operation_occurred
        ON shared.audit_entry (organization_id, operation, occurred_at)
    """,
)

_AUDIT_ENVELOPE = (
    """
    CREATE FUNCTION shared.vigia_canonical_audit_envelope(entry shared.audit_entry)
        RETURNS bytea
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
    AS $$
        SELECT convert_to(
            '{"actor":' || ledger.vigia_canonical_actor(
                    entry.actor_kind, entry.actor_id, entry.actor_display_name_snapshot,
                    entry.actor_role_in_use, entry.actor_concession_id, entry.actor_unit)
                || ',"chain_sequence":' || ledger.vigia_json_integer(entry.chain_sequence)
                || ',"correlation_id":' || ledger.vigia_json_uuid(entry.correlation_id)
                || ',"entry_id":' || ledger.vigia_json_uuid(entry.entry_id)
                || ',"filters_hash":' || ledger.vigia_json_text(entry.filters_hash)
                || ',"occurred_at":' || ledger.vigia_json_timestamp(entry.occurred_at)
                || ',"operation":' || ledger.vigia_json_text(entry.operation)
                || ',"organization_id":' || ledger.vigia_json_uuid(entry.organization_id)
                || ',"outcome":' || ledger.vigia_json_text(entry.outcome)
                || ',"resource_ref":' || CASE
                    WHEN entry.resource_kind IS NULL AND entry.resource_id IS NULL THEN 'null'
                    ELSE '{"id":' || ledger.vigia_json_uuid(entry.resource_id)
                        || ',"kind":' || ledger.vigia_json_text(entry.resource_kind) || '}'
                    END
                || ',"result_count":' || ledger.vigia_json_integer(entry.result_count)
                || ',"scope":{"plant_id":' || ledger.vigia_json_uuid(entry.scope_plant_id)
                || ',"zone_id":' || ledger.vigia_json_uuid(entry.scope_zone_id)
                || '}}',
            'UTF8'
        )
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_canonical_audit_envelope(shared.audit_entry) IS
        'Bytes RFC 8785 del sobre de una entrada de auditoría con forma fija (BR-NUC-60, PR-NUC-47)'
    """,
    """
    CREATE TRIGGER audit_entry_chain_link
        BEFORE INSERT ON shared.audit_entry
        FOR EACH ROW EXECUTE FUNCTION ledger.vigia_chain_link('audit')
    """,
    "ALTER TABLE shared.audit_entry ENABLE ALWAYS TRIGGER audit_entry_chain_link",
)

_OUTBOX = (
    """
    CREATE TABLE shared.outbox_event (
        event_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid,
        event_name text NOT NULL
            CONSTRAINT outbox_event_event_name REFERENCES shared.event_type (event_name),
        partition_key text NOT NULL GENERATED ALWAYS AS (
            organization_id::text || ':' || coalesce(plant_id::text, 'organization')
        ) STORED,
        ledger_sequence bigint
            CONSTRAINT outbox_event_ledger_sequence CHECK (ledger_sequence >= 1),
        payload jsonb NOT NULL
            CONSTRAINT outbox_event_payload
                CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 65536),
        correlation_id uuid NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    """
    COMMENT ON TABLE shared.outbox_event IS
        'Eventos de la bandeja de salida (OutboxEvent, domain-entities §4.3): solo anexar'
    """,
    """
    CREATE INDEX outbox_event_partition
        ON shared.outbox_event (organization_id, partition_key, created_at)
    """,
    f"""
    CREATE TABLE shared.outbox_delivery (
        event_id uuid NOT NULL
            CONSTRAINT outbox_delivery_event REFERENCES shared.outbox_event (event_id),
        consumer_name text NOT NULL
            CONSTRAINT outbox_delivery_consumer REFERENCES shared.consumer (consumer_name),
        organization_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'pending'
            CONSTRAINT outbox_delivery_status
                CHECK (status IN ('pending', 'retrying', 'delivered', 'dead_letter')),
        attempts integer NOT NULL DEFAULT 0
            CONSTRAINT outbox_delivery_attempts CHECK (attempts >= 0),
        next_attempt_at timestamptz NOT NULL,
        last_error_code text
            CONSTRAINT outbox_delivery_last_error_code CHECK (last_error_code ~ {_SNAKE64}),
        delivered_at timestamptz,
        CONSTRAINT outbox_delivery_pkey PRIMARY KEY (event_id, consumer_name),
        CONSTRAINT outbox_delivery_delivered
            CHECK ((status = 'delivered') = (delivered_at IS NOT NULL))
    )
    """,
    """
    COMMENT ON TABLE shared.outbox_delivery IS
        'Entrega de cada evento a cada consumidor (OutboxDelivery, domain-entities §4.3)'
    """,
    """
    CREATE INDEX outbox_delivery_due
        ON shared.outbox_delivery (organization_id, consumer_name, next_attempt_at)
        WHERE status IN ('pending', 'retrying')
    """,
    f"""
    CREATE TABLE shared.dead_letter (
        event_id uuid NOT NULL
            CONSTRAINT dead_letter_event REFERENCES shared.outbox_event (event_id),
        consumer_name text NOT NULL
            CONSTRAINT dead_letter_consumer REFERENCES shared.consumer (consumer_name),
        organization_id uuid NOT NULL,
        failed_at timestamptz NOT NULL,
        attempts integer NOT NULL
            CONSTRAINT dead_letter_attempts CHECK (attempts >= 1),
        last_error_code text NOT NULL
            CONSTRAINT dead_letter_last_error_code CHECK (last_error_code ~ {_SNAKE64}),
        replayed_at timestamptz,
        replayed_by uuid,
        CONSTRAINT dead_letter_pkey PRIMARY KEY (event_id, consumer_name, failed_at),
        CONSTRAINT dead_letter_replay CHECK ((replayed_at IS NULL) = (replayed_by IS NULL))
    )
    """,
    """
    COMMENT ON TABLE shared.dead_letter IS
        'Cola muerta de la bandeja de salida (DeadLetter, domain-entities §4.3): solo anexar'
    """,
)

_MONTH_PARTITIONS = f"""
DO $$
DECLARE
    first_month CONSTANT timestamp := date_trunc('month', now() AT TIME ZONE 'UTC');
    month_start timestamp;
BEGIN
    CREATE TABLE shared.audit_entry_default PARTITION OF shared.audit_entry DEFAULT;
    PERFORM shared.vigia_protect_append_only_partition('shared.audit_entry_default');
    FOR offset_months IN 0..{PARTITION_MONTHS_AHEAD} LOOP
        month_start := first_month + make_interval(months => offset_months);
        EXECUTE format(
            'CREATE TABLE shared.%I PARTITION OF shared.audit_entry FOR VALUES FROM (%L) TO (%L)',
            'audit_entry' || to_char(month_start, '"_"YYYY"_"MM'),
            to_char(month_start, 'YYYY-MM-DD HH24:MI:SS') || '+00',
            to_char(month_start + interval '1 month', 'YYYY-MM-DD HH24:MI:SS') || '+00'
        );
        PERFORM shared.vigia_protect_append_only_partition(
            format('shared.%I', 'audit_entry' || to_char(month_start, '"_"YYYY"_"MM'))::regclass);
    END LOOP;
END
$$
"""

_CLIENT_TABLES = (
    "shared.audit_entry",
    "shared.audit_entry_identity",
    "shared.outbox_event",
    "shared.outbox_delivery",
    "shared.dead_letter",
)
_APPEND_ONLY_TABLES = (
    "shared.audit_entry",
    "shared.audit_entry_identity",
    "shared.outbox_event",
    "shared.dead_letter",
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
    "GRANT SELECT, INSERT ON shared.audit_entry TO vigia_app",
    "GRANT SELECT, INSERT ON shared.outbox_event TO vigia_app",
    "GRANT SELECT, INSERT, UPDATE ON shared.outbox_delivery TO vigia_app",
    "GRANT SELECT, INSERT ON shared.dead_letter TO vigia_app",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_AUDIT_ENTRY, *_AUDIT_ENVELOPE, *_OUTBOX):
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
