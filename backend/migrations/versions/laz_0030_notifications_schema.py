"""Esquema ``notifications``: avisos, entregas, lotes, preferencias y correos (TASK-339, LC-LAZ-02;
NFR-LAZ-07, 08 y 09; PAT-LAZ-ESC-02 y 04, PAT-LAZ-SEG-03; adenda A-69).

Revisión laz_0030, tercer eslabón previsto de U-04 tras ``laz_0029`` (``loop``). Las cinco
entidades de ``domain-entities.md`` §6.2 con sus dos notas fechadas (2026-09-20 y 2026-09-21).
Son **estado operativo** (§9): ninguna contiene una decisión del expediente, y por eso, y solo por
eso, tienen retención declarada. Los servicios (TASK-324, 325 y 348) solo escriben repositorios
encima.

- **Ningún campo silencia ``immediate``** (BR-LAZ-126): ``user_notification_preference`` solo tiene
  ``email_enabled_normal``, ``email_enabled_digest``, el día del resumen y las dos marcas de
  supresión (rebote duro y queja), que solo afectan a ``normal`` y ``digest``. La imposibilidad es
  estructural: la prueba compara el conjunto de columnas con la lista del diseño.
- **Claves de idempotencia en tablas sin particionar.** PostgreSQL exige que una restricción única
  de una tabla particionada incluya la columna de partición, así que ni
  ``(organization_id, source_event_id)`` en ``notification`` (por ``created_at``) ni
  ``provider_message_id`` en ``email_message`` (por ``queued_at``) se pueden declarar únicos
  allí. Como ``ledger.record_source_key`` (nuc_0002), la unicidad vive en
  ``notification_source_key`` y ``email_provider_message_key``, que llena un disparador
  ``SECURITY DEFINER`` en el mismo ``INSERT`` (o al fijar ``provider_message_id``): una clave
  repetida falla con ``unique_violation`` (``23505``) y aborta la sentencia. La clave del aviso
  lleva el destinatario, ``(organization_id, source_event_id, recipient_user_id)``: un evento
  avisa a cada usuario con alcance (BR-LAZ-124) y el mismo evento reentregado no duplica el aviso
  de ninguno (BR-LAZ-129, PR-LAZ-40). ``provider_message_id`` se fija una sola vez (PR-LAZ-58).
- Toda tabla lleva ``organization_id``; el destinatario es un usuario **de esa organización**
  (clave foránea a ``identity.user_account (organization_id, user_id)``). ``notification`` y
  ``notification_delivery`` llevan ``plant_id`` y ``zone_id`` opcionales (zona solo con planta,
  claves foráneas a ``identity.plant`` y ``identity.zone``); la entrega copia los de su aviso y un
  disparador lo comprueba. Las marcas no tienen valor por defecto: las pone la aplicación con su
  ``Clock``.
- **Seguridad a nivel de fila** (``migrations/laz_policies.py``): ``organization_isolation``
  forzada en las siete; ``provider_concession_scope`` RESTRICTIVE en ``notification``,
  ``notification_delivery`` y ``notification_source_key`` (planta opcional: sin planta, solo una
  concesión de toda la organización la ve); y ``provider_context_denied`` RESTRICTIVE (A-69) en
  ``user_notification_preference``, ``digest_batch``, ``email_message`` y
  ``email_provider_message_key``: bajo cualquier concesión, ninguna fila visible ni escribible. La
  restricción a «solo las mías» dentro de la organización es de la aplicación (PAT-LAZ-SEG-03).
- **Particiones mensuales** (UTC, NFR-LAZ-07): ``notification`` por ``created_at``,
  ``notification_delivery`` por ``notification_created_at`` (la marca de su aviso: la entrega nace
  con él, así la clave foránea compuesta y el desprendimiento a los 24 meses van alineados por mes)
  y ``email_message`` por ``queued_at``, con partición por defecto; la migración crea el mes en
  curso y los tres siguientes. ``create_partitions`` las mantiene con dos funciones **nuevas**,
  ``shared.vigia_create_notifications_month_partitions`` y
  ``shared.vigia_notifications_default_partition_rows`` (mismo cuerpo, guarda de actor y candado
  que las de ``loop``): las de nuc_0016, gob_0018 y laz_0029 no se tocan, porque la imagen N-1
  convierte cada fila que devuelven a su ``PartitionedTable`` (NFR-NUC-14). Cada partición nace
  protegida contra el vaciado (``shared.vigia_protect_append_only_partition``) como todas las de
  ``PartitionedTable``: la retención **desprende** la partición, no la vacía.
- **Índices** (PAT-LAZ-ESC-02): ``notification_recipient_list``, la lista del usuario por clave
  ``(organization_id, recipient_user_id, created_at DESC, notification_id)`` sin lo archivado;
  ``email_message_resend_rate`` ``(organization_id, to_user_id, queued_at)`` para el límite exacto
  de reenvío; ``notification_delivery_digest`` ``(digest_batch_id)``.
- ``vigia_app``: ``SELECT`` e ``INSERT`` en las cinco entidades, ``UPDATE`` solo en las columnas
  que el diseño deja cambiar (``app_updatable_columns``) y solo ``SELECT`` en las tablas de clave;
  nunca ``DELETE`` ni ``TRUNCATE``.
- **Retención** (NFR-LAZ-08, nota de §6.2): ``COMMENT ON TABLE`` declara la categoría
  (``RETENTION``) y el plazo (``RETENTION_PERIODS``): aviso y entrega 24 meses, correo metadatos
  24 meses y cuerpo 90 días (``body_es`` admite nulo solo para eso), lote 12 meses y preferencias
  sin caducidad. Las tablas de clave caducan con su tabla. La tarea de purga no existe en v1.

Lo que no hace: el motor, la agrupación, el lector, las preferencias ni el correo (TASK-324, 348 y
325), el archivado a los 60 días (TASK-348) ni subir ``MINIMUM_SCHEMA_VERSION``.
"""

from __future__ import annotations

from typing import Final

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0030"
down_revision: str | None = "laz_0029"
branch_labels: None = None
depends_on: None = None

SCHEMA: Final = "notifications"
PARTITION_MONTHS_AHEAD: Final = 3
"""Meses siguientes al actual que la migración deja creados (PAT-NUC-ESC-01)."""

_URGENCY = "'immediate', 'normal', 'digest'"
"""``notification_urgency`` (§1)."""
_CHANNEL = "'in_app', 'email'"
"""``notification_channel`` (§1)."""
_DELIVERY_STATUS = "'pending', 'sent', 'failed', 'suppressed_by_preference', 'degraded_app_only'"
"""``notification_delivery_status`` (§1)."""
_DEEP_LINK_KIND = "'finding', 'queue_item', 'export', 'package', 'zone', 'node'"
_UUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
_CODE = "'^[a-z][a-z0-9_]*$'"
"""Identificador en inglés, minúsculas y guion bajo: estados y códigos que el diseño no cierra
(``DigestBatch.status``, ``EmailMessage.status``, ``failure_code``). La lista la fija el código
de TASK-325 y 348; la base solo impide texto libre."""


def _length(column: str, low: int, high: int) -> str:
    return f"CHECK (char_length({column}) BETWEEN {low} AND {high})"


def _code(column: str, high: int) -> str:
    return f"CHECK (char_length({column}) <= {high} AND {column} ~ {_CODE})"


def _ids(column: str, high: int) -> str:
    return (
        f"CHECK (CASE WHEN COALESCE(array_ndims({column}), 1) = 1"
        f" THEN cardinality({column}) <= {high} AND array_position({column}, NULL) IS NULL"
        f" ELSE false END)"
    )


_TABLES = (
    # §6.2 Notification 🔒: el aviso en la aplicación; título y cuerpo ya validados.
    f"""
    CREATE TABLE notifications.notification (
        notification_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid,
        zone_id uuid,
        event_name text NOT NULL
            CONSTRAINT notification_event_name_format {_code("event_name", 64)},
        source_event_id uuid NOT NULL,
        urgency text NOT NULL
            CONSTRAINT notification_urgency_values CHECK (urgency IN ({_URGENCY})),
        title_es text NOT NULL CONSTRAINT notification_title_length {_length("title_es", 1, 120)},
        body_es text NOT NULL CONSTRAINT notification_body_length {_length("body_es", 1, 1000)},
        deep_link_ref jsonb NOT NULL
            CONSTRAINT notification_deep_link_ref CHECK (
                jsonb_typeof(deep_link_ref) = 'object'
                AND deep_link_ref - 'kind' - 'id' = '{{}}'::jsonb
                AND jsonb_typeof(deep_link_ref -> 'kind') = 'string'
                AND deep_link_ref ->> 'kind' IN ({_DEEP_LINK_KIND})
                AND jsonb_typeof(deep_link_ref -> 'id') = 'string'
                AND deep_link_ref ->> 'id' ~ {_UUID}
            ),
        recipient_user_id uuid NOT NULL,
        created_at timestamptz NOT NULL,
        read_at timestamptz,
        archived_at timestamptz,
        CONSTRAINT notification_pkey PRIMARY KEY (notification_id, created_at),
        CONSTRAINT notification_scope UNIQUE (organization_id, notification_id, created_at),
        CONSTRAINT notification_recipient_fkey FOREIGN KEY (organization_id, recipient_user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT notification_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT notification_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT notification_zone_needs_plant CHECK (zone_id IS NULL OR plant_id IS NOT NULL),
        CONSTRAINT notification_read_after_creation CHECK (read_at >= created_at),
        CONSTRAINT notification_archived_after_creation CHECK (archived_at >= created_at)
    ) PARTITION BY RANGE (created_at)
    """,
    """
    CREATE INDEX notification_recipient_list ON notifications.notification
        (organization_id, recipient_user_id, created_at DESC, notification_id)
        WHERE archived_at IS NULL
    """,
    """
    CREATE INDEX notification_scope_source_event
        ON notifications.notification (organization_id, source_event_id)
    """,
    # La unicidad de (organización, evento, destinatario): la llena el disparador del aviso.
    """
    CREATE TABLE notifications.notification_source_key (
        organization_id uuid NOT NULL,
        source_event_id uuid NOT NULL,
        recipient_user_id uuid NOT NULL,
        plant_id uuid,
        notification_id uuid NOT NULL,
        created_at timestamptz NOT NULL,
        CONSTRAINT notification_source_key_pkey
            PRIMARY KEY (organization_id, source_event_id, recipient_user_id)
    )
    """,
    # §6.2 DigestBatch 🔒: un lote de correo agrupado o el resumen semanal del gerente.
    f"""
    CREATE TABLE notifications.digest_batch (
        digest_batch_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        recipient_user_id uuid NOT NULL,
        kind text NOT NULL
            CONSTRAINT digest_batch_kind_values CHECK (kind IN ('email_grouped', 'weekly_manager')),
        window_from timestamptz NOT NULL,
        window_to timestamptz NOT NULL,
        notification_ids uuid[] NOT NULL
            CONSTRAINT digest_batch_notification_ids {_ids("notification_ids", 10000)},
        item_count integer NOT NULL
            CONSTRAINT digest_batch_item_count CHECK (item_count = cardinality(notification_ids)),
        sent_at timestamptz,
        status text NOT NULL CONSTRAINT digest_batch_status_format {_code("status", 32)},
        CONSTRAINT digest_batch_scope UNIQUE (organization_id, digest_batch_id),
        CONSTRAINT digest_batch_recipient_fkey FOREIGN KEY (organization_id, recipient_user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT digest_batch_window CHECK (window_to > window_from),
        CONSTRAINT digest_batch_sent_after_window_start CHECK (sent_at >= window_from)
    )
    """,
    """
    CREATE INDEX digest_batch_scope_recipient
        ON notifications.digest_batch (organization_id, recipient_user_id, kind, window_from)
    """,
    # §6.2 NotificationDelivery 🔒: una entrega por canal, con la planta y la zona de su aviso.
    f"""
    CREATE TABLE notifications.notification_delivery (
        delivery_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        plant_id uuid,
        zone_id uuid,
        notification_id uuid NOT NULL,
        notification_created_at timestamptz NOT NULL,
        channel text NOT NULL
            CONSTRAINT notification_delivery_channel_values CHECK (channel IN ({_CHANNEL})),
        status text NOT NULL
            CONSTRAINT notification_delivery_status_values CHECK (status IN ({_DELIVERY_STATUS})),
        attempts integer NOT NULL DEFAULT 0
            CONSTRAINT notification_delivery_attempts CHECK (attempts >= 0),
        scheduled_for timestamptz,
        sent_at timestamptz,
        failure_code text
            CONSTRAINT notification_delivery_failure_code_format {_code("failure_code", 64)},
        digest_batch_id uuid,
        provider_message_id text
            CONSTRAINT notification_delivery_provider_message_id_length
                {_length("provider_message_id", 1, 128)},
        CONSTRAINT notification_delivery_pkey PRIMARY KEY (delivery_id, notification_created_at),
        CONSTRAINT notification_delivery_notification_fkey
            FOREIGN KEY (organization_id, notification_id, notification_created_at)
            REFERENCES notifications.notification (organization_id, notification_id, created_at),
        CONSTRAINT notification_delivery_digest_batch_fkey
            FOREIGN KEY (organization_id, digest_batch_id)
            REFERENCES notifications.digest_batch (organization_id, digest_batch_id),
        CONSTRAINT notification_delivery_zone_needs_plant
            CHECK (zone_id IS NULL OR plant_id IS NOT NULL),
        CONSTRAINT notification_delivery_provider_message_only_email
            CHECK (channel = 'email' OR provider_message_id IS NULL)
    ) PARTITION BY RANGE (notification_created_at)
    """,
    """
    CREATE INDEX notification_delivery_digest ON notifications.notification_delivery
        (digest_batch_id) WHERE digest_batch_id IS NOT NULL
    """,
    """
    CREATE INDEX notification_delivery_scope_notification ON notifications.notification_delivery
        (organization_id, notification_id, notification_created_at)
    """,
    # §6.2 UserNotificationPreference 🔒: sin ningún campo para silenciar immediate (BR-LAZ-126).
    """
    CREATE TABLE notifications.user_notification_preference (
        user_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        email_enabled_normal boolean NOT NULL DEFAULT true,
        email_enabled_digest boolean NOT NULL DEFAULT true,
        weekly_digest_weekday integer NOT NULL
            CONSTRAINT user_notification_preference_weekday
                CHECK (weekly_digest_weekday BETWEEN 1 AND 7),
        updated_at timestamptz NOT NULL,
        email_suppressed_hard_bounce boolean NOT NULL DEFAULT false,
        email_suppressed_complaint boolean NOT NULL DEFAULT false,
        email_suppressed_at timestamptz,
        CONSTRAINT user_notification_preference_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT user_notification_preference_suppressed_has_date CHECK (
            (email_suppressed_hard_bounce OR email_suppressed_complaint)
                = (email_suppressed_at IS NOT NULL)
        )
    )
    """,
    # §6.2 EmailMessage 🔒: ningún adjunto, clip ni enlace público (H-32).
    f"""
    CREATE TABLE notifications.email_message (
        message_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        to_user_id uuid NOT NULL,
        subject_es text NOT NULL
            CONSTRAINT email_message_subject_length {_length("subject_es", 1, 120)},
        body_es text CONSTRAINT email_message_body_length {_length("body_es", 1, 8000)},
        kind text NOT NULL
            CONSTRAINT email_message_kind_values
                CHECK (kind IN ('notification', 'digest', 'platform_invitation')),
        related_ids uuid[] NOT NULL CONSTRAINT email_message_related_ids {_ids("related_ids", 200)},
        queued_at timestamptz NOT NULL,
        sent_at timestamptz,
        status text NOT NULL CONSTRAINT email_message_status_format {_code("status", 32)},
        failure_code text CONSTRAINT email_message_failure_code_format {_code("failure_code", 64)},
        provider_message_id text
            CONSTRAINT email_message_provider_message_id_length
                {_length("provider_message_id", 1, 128)},
        bounce_kind text
            CONSTRAINT email_message_bounce_kind_values CHECK (bounce_kind IN ('hard', 'soft')),
        complaint_at timestamptz,
        CONSTRAINT email_message_pkey PRIMARY KEY (message_id, queued_at),
        CONSTRAINT email_message_recipient_fkey FOREIGN KEY (organization_id, to_user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT email_message_sent_after_queued CHECK (sent_at >= queued_at)
    ) PARTITION BY RANGE (queued_at)
    """,
    """
    CREATE INDEX email_message_resend_rate
        ON notifications.email_message (organization_id, to_user_id, queued_at)
    """,
    # La unicidad de provider_message_id (idempotencia del consumidor de entrega, PR-LAZ-58).
    """
    CREATE TABLE notifications.email_provider_message_key (
        provider_message_id text PRIMARY KEY,
        organization_id uuid NOT NULL,
        message_id uuid NOT NULL,
        queued_at timestamptz NOT NULL
    )
    """,
)

ENTITY_TABLES: Final = (
    "notification",
    "notification_delivery",
    "digest_batch",
    "user_notification_preference",
    "email_message",
)
"""Las cinco entidades de ``domain-entities.md`` §6.2."""

KEY_TABLES: Final = ("notification_source_key", "email_provider_message_key")
"""Las claves de idempotencia sin particionar (las llenan los disparadores)."""

TABLES: Final = (*ENTITY_TABLES, *KEY_TABLES)
"""Las siete tablas de ``notifications``."""

ORGANIZATION_ONLY_TABLES: Final = (
    "digest_batch",
    "user_notification_preference",
    "email_message",
    "email_provider_message_key",
)
"""Las de solo organización según A-69: ninguna fila bajo una concesión de proveedor."""

PLANT_TABLES: Final = ("notification", "notification_delivery", "notification_source_key")
"""Las que llevan ``plant_id`` (opcional): ``provider_concession_scope`` acota la concesión."""

PARTITIONED_TABLES: Final = {
    "notification": "created_at",
    "notification_delivery": "notification_created_at",
    "email_message": "queued_at",
}
"""Tablas particionadas por mes (UTC) y su columna de partición (NFR-LAZ-07)."""

RETENTION: Final = {
    **{table: "operational_state_with_retention" for table in TABLES},
    "user_notification_preference": "operational_state",
}
"""Categoría de retención de cada tabla (NFR-LAZ-08, ``domain-entities.md`` §6.2 y §9)."""

RETENTION_PERIODS: Final = {
    "notification": "24 meses en línea; después se desprende la partición sin archivarla",
    "notification_delivery": "24 meses en línea; después se desprende la partición sin archivarla",
    "digest_batch": "12 meses",
    "user_notification_preference": "sin caducidad: configuración vigente del usuario",
    "email_message": "metadatos 24 meses y cuerpo (body_es) 90 días",
    "notification_source_key": "la de su aviso (24 meses)",
    "email_provider_message_key": "la de su correo (24 meses)",
}
"""Plazo declarado de cada tabla (nota fechada 2026-09-20 de §6.2)."""

_ENTITIES: Final = {
    "notification": "Notification (domain-entities §6.2)",
    "notification_delivery": "NotificationDelivery (domain-entities §6.2)",
    "digest_batch": "DigestBatch (domain-entities §6.2)",
    "user_notification_preference": (
        "UserNotificationPreference (domain-entities §6.2); sin campo para silenciar immediate"
        " (BR-LAZ-126)"
    ),
    "email_message": "EmailMessage (domain-entities §6.2)",
    "notification_source_key": (
        "Clave de idempotencia de Notification: (organización, evento, destinatario) única"
        " (BR-LAZ-129); la llena el disparador del aviso"
    ),
    "email_provider_message_key": (
        "Clave de idempotencia de EmailMessage: provider_message_id único (PR-LAZ-58); la llena"
        " el disparador del correo"
    ),
}

_MUTABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "notification": ("read_at", "archived_at"),
    "notification_delivery": (
        "status",
        "attempts",
        "scheduled_for",
        "sent_at",
        "failure_code",
        "digest_batch_id",
        "provider_message_id",
    ),
    "digest_batch": ("sent_at", "status"),
    "user_notification_preference": (
        "email_enabled_normal",
        "email_enabled_digest",
        "weekly_digest_weekday",
        "updated_at",
        "email_suppressed_hard_bounce",
        "email_suppressed_complaint",
        "email_suppressed_at",
    ),
    "email_message": (
        "sent_at",
        "status",
        "failure_code",
        "provider_message_id",
        "bounce_kind",
        "complaint_at",
    ),
}
"""Columnas que el diseño deja cambiar. Nunca claves, organización, planta, zona, destinatario ni
el contenido del aviso o del correo; las tablas de clave no tienen ninguna."""


def app_updatable_columns(table: str) -> tuple[str, ...]:
    """Columnas con ``UPDATE`` de ``vigia_app``."""
    return _MUTABLE_COLUMNS.get(table, ())


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


_KEY_FUNCTIONS = (
    # SECURITY DEFINER: vigia_app no escribe en las tablas de clave. La RLS forzada se aplica
    # también al dueño, así que la clave solo entra con la organización (y la planta) del
    # contexto, igual que la fila que la reclama.
    """
    CREATE FUNCTION notifications.claim_notification_source_key() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        INSERT INTO notifications.notification_source_key (
            organization_id, source_event_id, recipient_user_id, plant_id, notification_id,
            created_at
        ) VALUES (
            NEW.organization_id, NEW.source_event_id, NEW.recipient_user_id, NEW.plant_id,
            NEW.notification_id, NEW.created_at
        );
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE FUNCTION notifications.claim_email_provider_message_key() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.provider_message_id IS NOT DISTINCT FROM OLD.provider_message_id THEN
                RETURN NULL;
            END IF;
            IF OLD.provider_message_id IS NOT NULL THEN
                RAISE EXCEPTION 'provider_message_id se fija una sola vez'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        IF NEW.provider_message_id IS NOT NULL THEN
            INSERT INTO notifications.email_provider_message_key (
                provider_message_id, organization_id, message_id, queued_at
            ) VALUES (
                NEW.provider_message_id, NEW.organization_id, NEW.message_id, NEW.queued_at
            );
        END IF;
        RETURN NULL;
    END
    $$
    """,
    # La entrega lleva la planta y la zona de su aviso (la política de proveedor la acota igual).
    # Se lee bajo la RLS de quien escribe: un aviso que no ve no admite entregas. AFTER: la
    # política (WITH CHECK) decide antes, como con las claves foráneas. Planta, zona y aviso no
    # son actualizables, así que basta el INSERT.
    """
    CREATE FUNCTION notifications.check_delivery_scope() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT
            FROM notifications.notification AS n
            WHERE n.organization_id = NEW.organization_id
                AND n.notification_id = NEW.notification_id
                AND n.created_at = NEW.notification_created_at
                AND n.plant_id IS NOT DISTINCT FROM NEW.plant_id
                AND n.zone_id IS NOT DISTINCT FROM NEW.zone_id
        ) THEN
            RAISE EXCEPTION 'la entrega no lleva la planta y la zona de su aviso'
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        RETURN NULL;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION notifications.claim_notification_source_key() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION notifications.claim_email_provider_message_key() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION notifications.check_delivery_scope() FROM PUBLIC",
    """
    CREATE TRIGGER claim_source_key AFTER INSERT ON notifications.notification
        FOR EACH ROW EXECUTE FUNCTION notifications.claim_notification_source_key()
    """,
    """
    CREATE TRIGGER claim_provider_message_key
        AFTER INSERT OR UPDATE OF provider_message_id ON notifications.email_message
        FOR EACH ROW EXECUTE FUNCTION notifications.claim_email_provider_message_key()
    """,
    """
    CREATE TRIGGER check_delivery_scope AFTER INSERT ON notifications.notification_delivery
        FOR EACH ROW EXECUTE FUNCTION notifications.check_delivery_scope()
    """,
    # ENABLE ALWAYS: tampoco los salta session_replication_role = replica.
    "ALTER TABLE notifications.notification ENABLE ALWAYS TRIGGER claim_source_key",
    "ALTER TABLE notifications.email_message ENABLE ALWAYS TRIGGER claim_provider_message_key",
    "ALTER TABLE notifications.notification_delivery ENABLE ALWAYS TRIGGER check_delivery_scope",
)


# Las particiones, después de crear los índices de la tabla padre: nacen con los suyos.
_MONTH_PARTITIONS = f"""
DO $$
DECLARE
    first_month CONSTANT timestamp := date_trunc('month', now() AT TIME ZONE 'UTC');
    month_start timestamp;
    parent text;
BEGIN
    FOREACH parent IN ARRAY ARRAY[{", ".join(f"'{table}'" for table in PARTITIONED_TABLES)}] LOOP
        EXECUTE format('CREATE TABLE notifications.%I PARTITION OF notifications.%I DEFAULT',
                       parent || '_default', parent);
        PERFORM shared.vigia_protect_append_only_partition(
            format('notifications.%I', parent || '_default')::regclass);
        FOR offset_months IN 0..{PARTITION_MONTHS_AHEAD} LOOP
            month_start := first_month + make_interval(months => offset_months);
            EXECUTE format(
                'CREATE TABLE notifications.%I PARTITION OF notifications.%I'
                ' FOR VALUES FROM (%L) TO (%L)',
                parent || to_char(month_start, '"_"YYYY"_"MM'),
                parent,
                to_char(month_start, 'YYYY-MM-DD HH24:MI:SS') || '+00',
                to_char(month_start + interval '1 month', 'YYYY-MM-DD HH24:MI:SS') || '+00'
            );
            PERFORM shared.vigia_protect_append_only_partition(
                format('notifications.%I', parent || to_char(month_start, '"_"YYYY"_"MM'))
                    ::regclass);
        END LOOP;
    END LOOP;
END
$$
"""

_PARTITION_FUNCTIONS = (
    # Las funciones de nuc_0016, gob_0018 y laz_0029 no se tocan: la imagen N-1 convierte cada
    # fila que devuelven a su PartitionedTable y una fila de notifications haría fallar (y
    # revertir) su create_partitions (NFR-NUC-14). Mismo cuerpo, guarda de actor, candado y
    # protección contra el vaciado que las de loop.
    """
    CREATE FUNCTION shared.vigia_create_notifications_month_partitions(
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
        -- El mismo candado que vigia_create_month_partitions: todas se serializan entre sí.
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('vigia_create_month_partitions', 0));
        FOR target IN
            SELECT * FROM (VALUES
                ('notifications', 'notification', 'created_at'),
                ('notifications', 'notification_delivery', 'notification_created_at'),
                ('notifications', 'email_message', 'queued_at')
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
    COMMENT ON FUNCTION shared.vigia_create_notifications_month_partitions(date, date) IS
        'Particiones mensuales que falten de las tres tablas particionadas de notifications; '
        'salta el mes con filas en la partición por defecto (TASK-339)'
    """,
    """
    CREATE FUNCTION shared.vigia_notifications_default_partition_rows()
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
            SELECT 'notifications.notification', count(*)
                FROM notifications.notification_default
            UNION ALL
            SELECT 'notifications.notification_delivery', count(*)
                FROM notifications.notification_delivery_default
            UNION ALL
            SELECT 'notifications.email_message', count(*)
                FROM notifications.email_message_default;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_notifications_default_partition_rows() IS
        'Filas de cada partición por defecto de notifications (default_partition_rows, NFR-LAZ-07)'
    """,
)


def _grants() -> list[str]:
    statements = ["GRANT USAGE ON SCHEMA notifications TO vigia_app"]
    for signature in (
        "shared.vigia_create_notifications_month_partitions(date, date)",
        "shared.vigia_notifications_default_partition_rows()",
    ):
        statements += [
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
            f"GRANT EXECUTE ON FUNCTION {signature} TO vigia_app",
        ]
    for table in ENTITY_TABLES:
        statements.append(f"GRANT SELECT, INSERT ON notifications.{table} TO vigia_app")
        columns = app_updatable_columns(table)
        if columns:
            statements.append(
                f"GRANT UPDATE ({', '.join(columns)}) ON notifications.{table} TO vigia_app"
            )
    for table in KEY_TABLES:
        statements.append(f"GRANT SELECT ON notifications.{table} TO vigia_app")
    return statements


def _comments() -> list[str]:
    statements = [
        "COMMENT ON SCHEMA notifications IS 'Módulo notifications de U-04: avisos, entregas,"
        " lotes, preferencias y correos (estado operativo con retención declarada)'",
        "COMMENT ON INDEX notifications.notification_recipient_list IS"
        " 'Lista del usuario por clave, sin lo archivado (NFR-LAZ-09)'",
        "COMMENT ON INDEX notifications.email_message_resend_rate IS"
        " 'Límite exacto de reenvío por usuario y ventana (PAT-LAZ-ESC-02)'",
    ]
    for table in TABLES:
        provider = (
            "sin filas bajo concesión de proveedor (A-69)"
            if table in ORGANIZATION_ONLY_TABLES
            else "concesión de proveedor por planta"
        )
        statements.append(
            f"COMMENT ON TABLE notifications.{table} IS '{_ENTITIES[table]};"
            f" seguridad a nivel de fila forzada por organización y {provider};"
            f" retención: {RETENTION_PERIODS[table]}; {retention_tag(RETENTION[table])}'"
        )
    return statements


def upgrade() -> None:
    # Como en laz_0029: todo lo creado es de vigia_migrate, lo ejecute el maestro o él.
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute("CREATE SCHEMA notifications AUTHORIZATION vigia_migrate")
    for statement in (
        *_TABLES,
        _MONTH_PARTITIONS,
        *_PARTITION_FUNCTIONS,
        *_row_security(),
        *_KEY_FUNCTIONS,
        *_grants(),
        *_comments(),
    ):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
