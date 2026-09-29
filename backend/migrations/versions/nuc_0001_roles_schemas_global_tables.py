"""Roles, extensiones, esquemas y tablas globales (TASK-106, LC-NUC-12 parte 1).

Revisión nuc_0001, primer eslabón de la cadena única.

- Roles (NFR-NUC-20, PAT-NUC-SEG-05):
  - ``vigia_app``: la aplicación; ``LOGIN`` sin ``SUPERUSER``, ``CREATEDB``, ``CREATEROLE``,
    ``REPLICATION`` ni ``BYPASSRLS``; sin ``CREATE`` en la base ni en ningún esquema y sin tablas
    temporales; solo ``USAGE`` en los esquemas y los privilegios de tabla que se conceden uno a
    uno (nunca ``DELETE`` sobre las tablas globales).
  - ``vigia_migrate``: dueño de los esquemas y de todo lo que crean las migraciones; ``CREATE``
    en la base para los esquemas de U-03 y U-04; solo lo usa la tarea de despliegue.
  - Contraseñas de los secretos ``db/app`` y ``db/migrate`` (en local, de
    ``VIGIA_DB_APP_PASSWORD`` y ``VIGIA_DB_MIGRATE_PASSWORD``; ``shared.migration_credentials``),
    enviadas como verificador SCRAM-SHA-256 (``shared.role_passwords``). Si el rol ya existe en
    el clúster, se le fijan de nuevo ``LOGIN``, ``CREATEDB``, ``CREATEROLE``, ``INHERIT`` y la
    contraseña; si tiene ``SUPERUSER``, ``REPLICATION`` o ``BYPASSRLS``, o ``vigia_app`` es
    miembro de otro rol, la migración falla (un maestro sin ``SUPERUSER`` no puede quitarlos).
  - Funciona con un usuario maestro sin ``SUPERUSER`` (``vigia_owner`` de RDS: ``CREATEROLE``,
    ``CREATEDB`` y dueño de la base).
  - ``PUBLIC`` pierde ``CONNECT`` y ``TEMPORARY`` en la base y ``CREATE`` en ``public``.
- Extensiones ``pgcrypto`` (hashes del disparador de encadenado) y ``btree_gist`` (exclusión de
  ``ZoneNodeAssignment``, pendiente nº 36, adenda A-12 y A-32), en ``public``.
- Esquemas ``identity``, ``ledger`` y ``shared``, de ``vigia_migrate``.
- Tablas globales, sin datos de cliente ni seguridad a nivel de fila (domain-entities §6):
  ``identity.data_region`` (con ``us-east-1``), ``ledger.record_type``, ``shared.event_type``,
  ``shared.consumer`` y ``shared.periodic_task`` (con ``next_run_at``, ``lease_owner`` y
  ``lease_until``, nota de §4.3).
- ``shared.vigia_schema_version()``: número del último eslabón aplicado, que la aplicación compara
  al arrancar con su versión mínima (``vigia_platform.shared.schema_version``).

En AWS la ejecuta la tarea ``vigia-migrate`` con el usuario maestro en el primer despliegue
(``first_deploy=true``, infrastructure-design §5.4); las siguientes, con ``vigia_migrate``. Lo
que crea el usuario que ejecuta se crea con ``SET LOCAL ROLE vigia_migrate``, para que el dueño
sea siempre ``vigia_migrate``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

from vigia_platform.shared.role_passwords import role_password_verifier

revision: str = "nuc_0001"
down_revision: str | None = None
branch_labels: None = None
depends_on: None = None

_STAGE_VERIFIER = sa.text("SELECT set_config('vigia.role_verifier', :verifier, true)")
"""Deja el verificador en una variable de la transacción: el DDL de roles no admite parámetros."""

_CLEAR_VERIFIER = "SELECT set_config('vigia.role_verifier', '', true)"

# Un rol que ya existe solo se ajusta con lo que un maestro sin SUPERUSER puede cambiar (LOGIN,
# CREATEDB, CREATEROLE, INHERIT y la contraseña). Si tiene SUPERUSER, REPLICATION o BYPASSRLS, o
# vigia_app pertenece a otro rol (podría hacer SET ROLE), la migración falla: fallo cerrado.
_APP_ROLE = """
DO $$
DECLARE
    attributes CONSTANT text := 'LOGIN NOCREATEDB NOCREATEROLE NOINHERIT';
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'vigia_app') THEN
        IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'vigia_app'
                   AND (rolsuper OR rolreplication OR rolbypassrls))
           OR EXISTS (SELECT FROM pg_catalog.pg_auth_members AS m
                      JOIN pg_catalog.pg_roles AS r ON r.oid = m.member
                      WHERE r.rolname = 'vigia_app') THEN
            RAISE EXCEPTION 'vigia_app ya existe con SUPERUSER, REPLICATION, BYPASSRLS o como '
                'miembro de otro rol: corrígelo a mano antes de migrar';
        END IF;
        EXECUTE format('ALTER ROLE vigia_app WITH %s PASSWORD %L',
                       attributes, current_setting('vigia.role_verifier'));
    ELSE
        EXECUTE format('CREATE ROLE vigia_app WITH %s NOSUPERUSER NOREPLICATION NOBYPASSRLS '
                       'PASSWORD %L', attributes, current_setting('vigia.role_verifier'));
    END IF;
END
$$
"""

_MIGRATE_ROLE = """
DO $$
DECLARE
    attributes CONSTANT text := 'LOGIN NOCREATEDB NOCREATEROLE INHERIT';
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'vigia_migrate') THEN
        IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'vigia_migrate'
                   AND (rolsuper OR rolreplication OR rolbypassrls)) THEN
            RAISE EXCEPTION 'vigia_migrate ya existe con SUPERUSER, REPLICATION o BYPASSRLS: '
                'corrígelo a mano antes de migrar';
        END IF;
        EXECUTE format('ALTER ROLE vigia_migrate WITH %s PASSWORD %L',
                       attributes, current_setting('vigia.role_verifier'));
    ELSE
        EXECUTE format('CREATE ROLE vigia_migrate WITH %s NOSUPERUSER NOREPLICATION NOBYPASSRLS '
                       'PASSWORD %L', attributes, current_setting('vigia.role_verifier'));
    END IF;
END
$$
"""

_DATABASE_PRIVILEGES = """
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM vigia_app', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO vigia_app', current_database());
    EXECUTE format('GRANT CONNECT, CREATE ON DATABASE %I TO vigia_migrate', current_database());
END
$$
"""

_GLOBAL_TABLES = (
    """
    CREATE TABLE identity.data_region (
        region_code text PRIMARY KEY
            CONSTRAINT data_region_code_format CHECK (region_code ~ '^[a-z][a-z0-9-]{0,31}$'),
        description_es text NOT NULL
            CONSTRAINT data_region_description_length
                CHECK (char_length(description_es) BETWEEN 1 AND 200),
        country_hint text
            CONSTRAINT data_region_country_format CHECK (country_hint ~ '^[A-Z]{2}$')
    )
    """,
    """
    COMMENT ON TABLE identity.data_region IS
        'Regiones de datos admitidas (DataRegion, domain-entities §6); global, sin datos de cliente'
    """,
    """
    INSERT INTO identity.data_region (region_code, description_es, country_hint)
    VALUES ('us-east-1', 'Este de Estados Unidos (Norte de Virginia)', 'US')
    """,
    """
    CREATE TABLE ledger.record_type (
        record_type text PRIMARY KEY
            CONSTRAINT record_type_name_format CHECK (record_type ~ '^[a-z][a-z0-9_]{0,63}$'),
        writer_unit text NOT NULL
            CONSTRAINT record_type_writer_unit CHECK (writer_unit IN ('U-02', 'U-03', 'U-04')),
        chain_level text NOT NULL
            CONSTRAINT record_type_chain_level CHECK (chain_level IN ('plant', 'organization')),
        schema_version integer NOT NULL
            CONSTRAINT record_type_schema_version CHECK (schema_version >= 1),
        content_schema jsonb NOT NULL
            CONSTRAINT record_type_content_schema CHECK (jsonb_typeof(content_schema) = 'object'),
        source_key_path text
            CONSTRAINT record_type_source_key_path
                CHECK (char_length(source_key_path) BETWEEN 1 AND 256),
        free_text_paths text[] NOT NULL DEFAULT '{}',
        evidence_paths text[] NOT NULL DEFAULT '{}',
        label_rule jsonb
            CONSTRAINT record_type_label_rule CHECK (jsonb_typeof(label_rule) = 'object'),
        outbox_events text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    COMMENT ON TABLE ledger.record_type IS
        'Registro cerrado de tipos de registro (RecordType, domain-entities §3.3); global'
    """,
    """
    CREATE TABLE shared.event_type (
        event_name text PRIMARY KEY
            CONSTRAINT event_type_name_format CHECK (event_name ~ '^[a-z][a-z0-9_]{0,63}$'),
        publisher_unit text NOT NULL
            CONSTRAINT event_type_publisher_unit
                CHECK (publisher_unit IN ('U-02', 'U-03', 'U-04')),
        payload_schema jsonb NOT NULL
            CONSTRAINT event_type_payload_schema CHECK (jsonb_typeof(payload_schema) = 'object'),
        description_es text NOT NULL
            CONSTRAINT event_type_description_length
                CHECK (char_length(description_es) BETWEEN 1 AND 500)
    )
    """,
    """
    COMMENT ON TABLE shared.event_type IS
        'Eventos de la bandeja de salida (EventType, domain-entities §4.3); global'
    """,
    """
    CREATE TABLE shared.consumer (
        consumer_name text PRIMARY KEY
            CONSTRAINT consumer_name_format CHECK (consumer_name ~ '^[a-z][a-z0-9_]{0,63}$'),
        unit text NOT NULL
            CONSTRAINT consumer_unit CHECK (unit IN ('U-02', 'U-03', 'U-04')),
        subscribed_events text[] NOT NULL DEFAULT '{}',
        has_external_dependency boolean NOT NULL DEFAULT false,
        circuit_state text NOT NULL DEFAULT 'closed'
            CONSTRAINT consumer_circuit_state
                CHECK (circuit_state IN ('closed', 'open', 'half_open')),
        circuit_opened_at timestamptz,
        probe_interval_seconds integer NOT NULL DEFAULT 60
            CONSTRAINT consumer_probe_interval CHECK (probe_interval_seconds > 0)
    )
    """,
    """
    COMMENT ON TABLE shared.consumer IS
        'Consumidores de la bandeja de salida (Consumer, domain-entities §4.3); global'
    """,
    """
    CREATE TABLE shared.periodic_task (
        task_name text PRIMARY KEY
            CONSTRAINT periodic_task_name_format CHECK (task_name ~ '^[a-z][a-z0-9_]{0,63}$'),
        unit text NOT NULL
            CONSTRAINT periodic_task_unit CHECK (unit IN ('U-02', 'U-03', 'U-04')),
        schedule text NOT NULL
            CONSTRAINT periodic_task_schedule_length CHECK (char_length(schedule) BETWEEN 1 AND 64),
        iterates_organizations boolean NOT NULL DEFAULT true,
        last_run_at timestamptz,
        last_outcome text
            CONSTRAINT periodic_task_last_outcome_format
                CHECK (last_outcome ~ '^[a-z][a-z0-9_]{0,63}$'),
        next_run_at timestamptz NOT NULL,
        lease_owner text
            CONSTRAINT periodic_task_lease_owner_length
                CHECK (char_length(lease_owner) BETWEEN 1 AND 128),
        lease_until timestamptz,
        CONSTRAINT periodic_task_lease_complete
            CHECK ((lease_owner IS NULL) = (lease_until IS NULL))
    )
    """,
    """
    COMMENT ON TABLE shared.periodic_task IS
        'Tareas periódicas con arrendamiento (PeriodicTask, domain-entities §4.3); global'
    """,
)

_SCHEMA_VERSION_FUNCTION = """
CREATE FUNCTION shared.vigia_schema_version() RETURNS integer
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog
AS $$
    SELECT CASE
               WHEN count(*) = 1
                   THEN (max(substring(version_num FROM '^[a-z]{3}_([0-9]{4})$')))::integer
           END
    FROM public.alembic_version
$$
"""

_APP_GRANTS = (
    "GRANT USAGE ON SCHEMA identity, ledger, shared TO vigia_app",
    "GRANT SELECT ON identity.data_region TO vigia_app",
    # Cada unidad registra sus tipos, eventos, consumidores y tareas al arrancar; el worker
    # actualiza circuitos y arrendamientos. Nunca DELETE: retirar un registro está prohibido (P4).
    "GRANT SELECT, INSERT, UPDATE ON ledger.record_type TO vigia_app",
    "GRANT SELECT, INSERT, UPDATE ON shared.event_type TO vigia_app",
    "GRANT SELECT, INSERT, UPDATE ON shared.consumer TO vigia_app",
    "GRANT SELECT, INSERT, UPDATE ON shared.periodic_task TO vigia_app",
    "REVOKE ALL ON FUNCTION shared.vigia_schema_version() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION shared.vigia_schema_version() TO vigia_app",
    "GRANT SELECT ON public.alembic_version TO vigia_app",
)


def upgrade() -> None:
    # Antes de tocar la base: sin las dos contraseñas no se crea nada (fallo cerrado).
    passwords = context.config.attributes.get("role_passwords", {})
    app_verifier = role_password_verifier("vigia_app", passwords)
    migrate_verifier = role_password_verifier("vigia_migrate", passwords)

    op.execute(_STAGE_VERIFIER.bindparams(verifier=app_verifier))
    op.execute(_APP_ROLE)
    op.execute(_STAGE_VERIFIER.bindparams(verifier=migrate_verifier))
    op.execute(_MIGRATE_ROLE)
    op.execute(_CLEAR_VERIFIER)
    # Quien ejecuta la primera migración (el usuario maestro) puede actuar como vigia_migrate.
    op.execute("GRANT vigia_migrate TO CURRENT_USER")

    op.execute(_DATABASE_PRIVILEGES)
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public")

    op.execute("CREATE SCHEMA identity AUTHORIZATION vigia_migrate")
    op.execute("CREATE SCHEMA ledger AUTHORIZATION vigia_migrate")
    op.execute("CREATE SCHEMA shared AUTHORIZATION vigia_migrate")
    # Las migraciones siguientes corren como vigia_migrate: debe poder anotar su versión. Sin
    # SUPERUSER (el maestro de RDS), el nuevo dueño necesita CREATE en public solo para el cambio.
    op.execute("GRANT CREATE ON SCHEMA public TO vigia_migrate")
    op.execute("ALTER TABLE public.alembic_version OWNER TO vigia_migrate")
    op.execute("REVOKE CREATE ON SCHEMA public FROM vigia_migrate")

    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _GLOBAL_TABLES:
        op.execute(statement)
    op.execute(_SCHEMA_VERSION_FUNCTION)
    for statement in _APP_GRANTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
