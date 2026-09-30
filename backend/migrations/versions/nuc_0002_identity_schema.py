"""Esquema ``identity``: tenencia, usuarios, sesiones y concesiones (TASK-107, LC-NUC-12 parte 2).

Revisión nuc_0002. Las 18 tablas de ``domain-entities.md`` §2 con el aislamiento por
construcción de PAT-NUC-SEG-01 (capa 1, la base, y capa 2, la política de proveedor):

- Tablas, con el nombre de la entidad en ``snake_case``; ``User`` es ``identity.user_account``
  porque ``user`` es palabra reservada de PostgreSQL. Las marcas de tiempo no tienen valor por
  defecto: las pone la aplicación con su ``Clock`` (ningún módulo lee la hora del sistema).
  Las claves compuestas ``(organization_id, …)`` hacen que la base garantice que una zona, un
  nodo y una asignación son de la misma planta y organización (BR-NUC-08) y que un usuario, sus
  credenciales, sesiones, invitaciones y roles son de una sola organización (BR-NUC-05).
- ``FORCE ROW LEVEL SECURITY`` en las 18 (BR-NUC-01): la política ``organization_isolation``
  compara ``organization_id`` con ``vigia.organization_id``; sin la variable (o vacía, que es
  como queda al terminar el ``SET LOCAL``) no se ve ni se escribe ninguna fila. Ni el dueño
  (``vigia_migrate``) la omite; solo un superusuario, que la aplicación nunca usa.
- Segunda política, ``provider_concession_scope`` (restrictiva), en las tablas con ``plant_id``
  o ``zone_id`` (``plant``, ``zone``, ``node_identity``, ``zone_node_assignment``,
  ``live_view_token_issuance``): si ``vigia.actor_kind`` es del proveedor (``provider_user``, el
  valor de ``ActorKind``; también ``provider``, el nombre del diseño) o ``vigia.concession_id``
  no está vacía, solo pasan las filas dentro del alcance de esa concesión ``active``, sin
  revocar, ya en vigor y no vencida en ``now()`` (el inicio de la transacción). Sin concesión
  que cumpla, cero filas (PR-NUC-52). Fallo cerrado: basta una de las dos variables.
- Solo anexar (⛓): ``zone_node_assignment``, ``role_assignment``, ``provider_concession``,
  ``key_set_publication``, ``live_view_token_issuance`` y ``privacy_notice_acceptance``. Un
  disparador rechaza ``UPDATE``, ``DELETE`` y ``TRUNCATE`` para **cualquier** rol (PR-NUC-18) y
  ``vigia_app`` solo tiene ``SELECT`` e ``INSERT``. Única excepción, la **actualización de
  cierre**, una sola vez, sin cambiar ninguna otra columna, y con ``UPDATE`` de ``vigia_app``
  solo sobre esas columnas:

  - ``zone_node_assignment``: fija ``unassigned_at`` nulo;
  - ``role_assignment``: fija ``removed_at`` y ``removed_by`` nulos (la marca de fin de la
    entidad, §2.5);
  - ``provider_concession``: de ``active`` a ``revoked`` con ``revoked_at``, ``revoked_by`` y
    ``revoked_by_side``, o de ``active`` a ``expired`` sin ellos (§2.11); una concesión cerrada
    no vuelve a cambiar.

  Las otras tres no tienen marca de fin: ningún ``UPDATE``.
- Pendiente nº 36 (adenda A-12 y A-32): ``EXCLUDE USING gist (zone_id WITH =,
  tstzrange(assigned_at, unassigned_at) WITH &&)`` sobre las columnas existentes, sin columnas
  nuevas; el rango es ``[assigned_at, unassigned_at)``, así que dos asignaciones sucesivas que
  se tocan en un instante no se solapan. ``unassigned_at > assigned_at``: un rango vacío no
  chocaría con nada.
- ``node_identity.status`` admite ``re_enrollment_pending`` (re-alta del mismo ``node_id`` que
  pide U-03, junto al nº 36); ``live_view_local_url`` es nula hasta que el nodo la anuncia, con
  la forma canónica del contrato (pendiente nº 31, adenda A-35).
- Correo único en toda la plataforma y ya normalizado a minúsculas (BR-NUC-05); exactamente
  una organización ``provider`` (índice parcial único, BR-NUC-06); ``plant.data_region``
  inmutable (BR-NUC-07).
- ``vigia_app`` nunca tiene ``DELETE`` ni ``TRUNCATE`` en ``identity``; ``UPDATE`` solo sobre
  las columnas que el diseño deja cambiar (claves, organización y marcas de alta quedan fuera).

Lo que no hace: la lógica de aplicación de identidad (M3) y las tablas del expediente, la
auditoría y la bandeja (TASK-108).
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0002"
down_revision: str | None = "nuc_0001"
branch_labels: None = None
depends_on: None = None

_CODE = "^[A-Z0-9-]{2,32}$"
"""``code`` legible: 2 a 32 caracteres, mayúsculas, dígitos y guion (convenciones)."""

_SHA256_HEX = "^[0-9a-f]{64}$"

_LIVE_VIEW_URL = (
    r"^https://([A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*"
    r"|\[[0-9A-Fa-f:.]{2,45}\])\:8443/$"
)
"""Forma canónica de ``Heartbeat.live_view_local_url`` del contrato (adenda A-35).

``\\:`` escapa los dos puntos para ``op.execute`` (``text()``), que si no leería ``:8443`` como
parámetro; la sentencia llega a PostgreSQL con ``:8443``."""

_ROLES = (
    "'coordinator_sst', 'line_manager', 'plant_manager', 'administrator', "
    "'provider_installer', 'copasst', 'platform_operator'"
)

_FUNCTIONS = (
    # Capa 1: la organización de la transacción; NULL sin variable o con la variable vacía.
    """
    CREATE FUNCTION identity.rls_organization_id() RETURNS uuid
        LANGUAGE sql
        STABLE
    AS $$
        SELECT NULLIF(pg_catalog.current_setting('vigia.organization_id', true), '')::uuid
    $$
    """,
    # Capa 2: ¿puede la transacción ver o escribir una fila de esta organización y planta?
    """
    CREATE FUNCTION identity.rls_provider_scope_allows(row_organization_id uuid, row_plant_id uuid)
        RETURNS boolean
        LANGUAGE sql
        STABLE
    AS $$
        SELECT (
                COALESCE(pg_catalog.current_setting('vigia.actor_kind', true), '')
                    NOT IN ('provider', 'provider_user')
                AND COALESCE(pg_catalog.current_setting('vigia.concession_id', true), '') = ''
            )
            OR EXISTS (
                SELECT
                FROM identity.provider_concession AS concession
                WHERE concession.concession_id
                        = NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid
                    AND concession.organization_id = row_organization_id
                    AND concession.status = 'active'
                    AND concession.revoked_at IS NULL
                    AND concession.granted_at <= pg_catalog.now()
                    AND concession.expires_at > pg_catalog.now()
                    AND (
                        concession.scope_level = 'organization'
                        OR (concession.scope_level = 'plant' AND concession.scope_id = row_plant_id)
                    )
            )
    $$
    """,
    """
    CREATE FUNCTION identity.reject_append_only_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = format('%I.%I es de solo anexar: %s está prohibido (P4)',
                             TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP);
    END
    $$
    """,
    # Cierre de una asignación de nodo: fija unassigned_at una vez y nada más.
    """
    CREATE FUNCTION identity.guard_zone_node_assignment() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE'
           AND OLD.unassigned_at IS NULL
           AND NEW.unassigned_at IS NOT NULL
           AND to_jsonb(NEW) - 'unassigned_at' = to_jsonb(OLD) - 'unassigned_at' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = format('identity.zone_node_assignment es de solo anexar: %s está prohibido'
                             ' salvo fijar una vez unassigned_at nulo (P4)', TG_OP);
    END
    $$
    """,
    # Retiro de un rol: fija removed_at y removed_by una vez y nada más.
    """
    CREATE FUNCTION identity.guard_role_assignment() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE'
           AND OLD.removed_at IS NULL
           AND NEW.removed_at IS NOT NULL
           AND to_jsonb(NEW) - ARRAY['removed_at', 'removed_by']
               = to_jsonb(OLD) - ARRAY['removed_at', 'removed_by'] THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = format('identity.role_assignment es de solo anexar: %s está prohibido'
                             ' salvo fijar una vez removed_at y removed_by nulos (P4)', TG_OP);
    END
    $$
    """,
    # Cierre de una concesión: de active a revoked (con revoked_*) o a expired, una sola vez.
    """
    CREATE FUNCTION identity.guard_provider_concession() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    DECLARE
        closing CONSTANT text[] := ARRAY['status', 'revoked_at', 'revoked_by', 'revoked_by_side'];
    BEGIN
        IF TG_OP = 'UPDATE'
           AND OLD.status = 'active'
           AND OLD.revoked_at IS NULL
           AND NEW.status IN ('revoked', 'expired')
           AND to_jsonb(NEW) - closing = to_jsonb(OLD) - closing THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = format('identity.provider_concession es de solo anexar: %s está prohibido'
                             ' salvo cerrar una vez una concesión activa (P4)', TG_OP);
    END
    $$
    """,
    """
    CREATE FUNCTION identity.reject_data_region_change() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF NEW.data_region IS DISTINCT FROM OLD.data_region THEN
            RAISE EXCEPTION USING
                ERRCODE = 'restrict_violation',
                MESSAGE = 'identity.plant.data_region es inmutable en este ciclo (BR-NUC-07)';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    # Los roles del proveedor, solo en la proveedora; los otros cinco, solo en clientes (§2.5).
    # Lee la organización con los permisos de quien inserta: la política la deja ver.
    """
    CREATE FUNCTION identity.check_role_organization_kind() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    DECLARE
        organization_kind text;
    BEGIN
        SELECT kind INTO organization_kind
        FROM identity.organization
        WHERE organization_id = NEW.organization_id;
        IF organization_kind IS NULL
           OR (NEW.role IN ('provider_installer', 'platform_operator'))
              <> (organization_kind = 'provider') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                MESSAGE = format('el rol %s no es asignable en una organización %s',
                                 NEW.role, COALESCE(organization_kind, 'no visible'));
        END IF;
        RETURN NEW;
    END
    $$
    """,
    # Una concesión siempre es sobre una organización cliente (§2.11).
    """
    CREATE FUNCTION identity.check_concession_client() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF NOT EXISTS (SELECT FROM identity.organization
                       WHERE organization_id = NEW.organization_id AND kind = 'client') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                MESSAGE = 'una concesión solo se otorga sobre una organización cliente visible';
        END IF;
        RETURN NEW;
    END
    $$
    """,
)

_TABLES = (
    f"""
    CREATE TABLE identity.organization (
        organization_id uuid PRIMARY KEY,
        code text NOT NULL
            CONSTRAINT organization_code_format CHECK (code ~ '{_CODE}')
            CONSTRAINT organization_code_unique UNIQUE,
        name text NOT NULL
            CONSTRAINT organization_name_length CHECK (char_length(name) BETWEEN 1 AND 120),
        kind text NOT NULL
            CONSTRAINT organization_kind_values CHECK (kind IN ('client', 'provider')),
        status text NOT NULL DEFAULT 'active'
            CONSTRAINT organization_status_values CHECK (status IN ('active', 'suspended')),
        concession_max_days integer NOT NULL DEFAULT 30
            CONSTRAINT organization_concession_max_days
                CHECK (concession_max_days BETWEEN 1 AND 90),
        concession_default_days integer NOT NULL DEFAULT 7
            CONSTRAINT organization_concession_default_days CHECK (concession_default_days >= 1),
        created_at timestamptz NOT NULL,
        created_by uuid NOT NULL,
        CONSTRAINT organization_concession_default_within_max
            CHECK (concession_default_days <= concession_max_days)
    )
    """,
    """
    CREATE UNIQUE INDEX organization_single_provider
        ON identity.organization (kind) WHERE kind = 'provider'
    """,
    """
    CREATE TABLE identity.user_account (
        user_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        email text NOT NULL
            CONSTRAINT user_account_email_length CHECK (char_length(email) BETWEEN 3 AND 254)
            CONSTRAINT user_account_email_normalized
                CHECK (email = lower(email) AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$')
            CONSTRAINT user_account_email_unique UNIQUE,
        display_name text NOT NULL
            CONSTRAINT user_account_display_name_length
                CHECK (char_length(display_name) BETWEEN 1 AND 120),
        professional_license text
            CONSTRAINT user_account_professional_license_length
                CHECK (char_length(professional_license) BETWEEN 1 AND 64),
        status text NOT NULL DEFAULT 'invited'
            CONSTRAINT user_account_status_values
                CHECK (status IN ('invited', 'active', 'deactivated')),
        second_factor_required boolean NOT NULL DEFAULT false,
        second_factor_enrolled_at timestamptz,
        password_updated_at timestamptz,
        last_login_at timestamptz,
        created_at timestamptz NOT NULL,
        deactivated_at timestamptz,
        privacy_notice_version_accepted text
            CONSTRAINT user_account_privacy_notice_version_length
                CHECK (char_length(privacy_notice_version_accepted) BETWEEN 1 AND 32),
        CONSTRAINT user_account_deactivated_has_date
            CHECK (status <> 'deactivated' OR deactivated_at IS NOT NULL),
        CONSTRAINT user_account_organization_user UNIQUE (organization_id, user_id)
    )
    """,
    # El alta de la proveedora y de su primer operador van en la misma transacción.
    """
    ALTER TABLE identity.organization
        ADD CONSTRAINT organization_created_by_fkey FOREIGN KEY (created_by)
            REFERENCES identity.user_account (user_id) DEFERRABLE INITIALLY DEFERRED
    """,
    f"""
    CREATE TABLE identity.plant (
        plant_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        code text NOT NULL CONSTRAINT plant_code_format CHECK (code ~ '{_CODE}'),
        name text NOT NULL
            CONSTRAINT plant_name_length CHECK (char_length(name) BETWEEN 1 AND 120),
        country text NOT NULL CONSTRAINT plant_country_format CHECK (country ~ '^[A-Z]{{2}}$'),
        data_region text NOT NULL REFERENCES identity.data_region (region_code),
        timezone text NOT NULL
            CONSTRAINT plant_timezone_format CHECK (timezone ~ '^[A-Za-z0-9_+/-]{{1,64}}$'),
        status text NOT NULL DEFAULT 'active'
            CONSTRAINT plant_status_values CHECK (status IN ('active', 'inactive')),
        created_at timestamptz NOT NULL,
        created_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        CONSTRAINT plant_code_unique UNIQUE (organization_id, code),
        CONSTRAINT plant_organization_plant UNIQUE (organization_id, plant_id)
    )
    """,
    f"""
    CREATE TABLE identity.zone (
        zone_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        code text NOT NULL CONSTRAINT zone_code_format CHECK (code ~ '{_CODE}'),
        name text NOT NULL CONSTRAINT zone_name_length CHECK (char_length(name) BETWEEN 1 AND 120),
        created_at timestamptz NOT NULL,
        created_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        CONSTRAINT zone_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT zone_code_unique UNIQUE (plant_id, code),
        CONSTRAINT zone_organization_plant_zone UNIQUE (organization_id, plant_id, zone_id)
    )
    """,
    f"""
    CREATE TABLE identity.node_identity (
        node_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        code text NOT NULL CONSTRAINT node_identity_code_format CHECK (code ~ '{_CODE}'),
        status text NOT NULL DEFAULT 'declared'
            CONSTRAINT node_identity_status_values
                CHECK (status IN ('declared', 'enrolled', 'revoked', 're_enrollment_pending')),
        live_view_local_url text
            CONSTRAINT node_identity_live_view_local_url_format
                CHECK (char_length(live_view_local_url) <= 256
                       AND live_view_local_url ~ '{_LIVE_VIEW_URL}'),
        created_at timestamptz NOT NULL,
        CONSTRAINT node_identity_plant_fkey FOREIGN KEY (organization_id, plant_id)
            REFERENCES identity.plant (organization_id, plant_id),
        CONSTRAINT node_identity_code_unique UNIQUE (organization_id, code),
        CONSTRAINT node_identity_organization_plant_node UNIQUE (organization_id, plant_id, node_id)
    )
    """,
    """
    CREATE TABLE identity.zone_node_assignment (
        assignment_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        assigned_at timestamptz NOT NULL,
        unassigned_at timestamptz,
        assigned_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        CONSTRAINT zone_node_assignment_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT zone_node_assignment_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT zone_node_assignment_interval CHECK (unassigned_at > assigned_at),
        CONSTRAINT zone_node_assignment_one_node_per_zone EXCLUDE USING gist (
            zone_id WITH =,
            tstzrange(assigned_at, unassigned_at) WITH &&
        )
    )
    """,
    "CREATE INDEX zone_node_assignment_node ON identity.zone_node_assignment (node_id)",
    f"""
    CREATE TABLE identity.role_assignment (
        assignment_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        user_id uuid NOT NULL,
        role text NOT NULL CONSTRAINT role_assignment_role_values CHECK (role IN ({_ROLES})),
        scope_level text NOT NULL
            CONSTRAINT role_assignment_scope_level_values
                CHECK (scope_level IN ('organization', 'plant', 'zone')),
        scope_id uuid NOT NULL,
        assigned_at timestamptz NOT NULL,
        assigned_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        removed_at timestamptz,
        removed_by uuid REFERENCES identity.user_account (user_id),
        CONSTRAINT role_assignment_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT role_assignment_organization_scope
            CHECK (scope_level <> 'organization' OR scope_id = organization_id),
        CONSTRAINT role_assignment_removal_complete
            CHECK ((removed_at IS NULL) = (removed_by IS NULL)),
        CONSTRAINT role_assignment_removal_after_assignment CHECK (removed_at >= assigned_at)
    )
    """,
    "CREATE INDEX role_assignment_user ON identity.role_assignment (user_id)",
    """
    CREATE TABLE identity.password_credential (
        user_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        password_hash text NOT NULL
            CONSTRAINT password_credential_hash_length
                CHECK (char_length(password_hash) BETWEEN 1 AND 512),
        algorithm_version text NOT NULL
            CONSTRAINT password_credential_algorithm_version_format
                CHECK (algorithm_version ~ '^[a-z0-9][a-z0-9_.$=,-]{0,63}$'),
        updated_at timestamptz NOT NULL,
        breach_checked_at timestamptz,
        CONSTRAINT password_credential_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id)
    )
    """,
    # Cifrado de sobre (PAT-NUC-SEG-04): en la fila viajan el texto cifrado y la clave envuelta.
    """
    CREATE TABLE identity.totp_credential (
        user_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        secret_encrypted bytea NOT NULL
            CONSTRAINT totp_credential_secret_length
                CHECK (octet_length(secret_encrypted) BETWEEN 1 AND 1024),
        data_key_wrapped bytea NOT NULL
            CONSTRAINT totp_credential_data_key_length
                CHECK (octet_length(data_key_wrapped) BETWEEN 1 AND 1024),
        enrolled_at timestamptz NOT NULL,
        last_accepted_step bigint CONSTRAINT totp_credential_step CHECK (last_accepted_step >= 0),
        disabled_at timestamptz,
        CONSTRAINT totp_credential_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id)
    )
    """,
    """
    CREATE TABLE identity.recovery_code (
        recovery_code_id uuid PRIMARY KEY,
        user_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        code_hash text NOT NULL
            CONSTRAINT recovery_code_hash_length CHECK (char_length(code_hash) BETWEEN 1 AND 512),
        generated_at timestamptz NOT NULL,
        used_at timestamptz,
        CONSTRAINT recovery_code_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT recovery_code_used_after_generation CHECK (used_at >= generated_at)
    )
    """,
    "CREATE INDEX recovery_code_user ON identity.recovery_code (user_id)",
    f"""
    CREATE TABLE identity.session (
        session_id_hash text PRIMARY KEY
            CONSTRAINT session_id_hash_format CHECK (session_id_hash ~ '{_SHA256_HEX}'),
        user_id uuid NOT NULL,
        organization_id uuid NOT NULL,
        created_at timestamptz NOT NULL,
        last_seen_at timestamptz NOT NULL,
        idle_expires_at timestamptz NOT NULL,
        absolute_expires_at timestamptz NOT NULL,
        second_factor_verified boolean NOT NULL DEFAULT false,
        client_hint text
            CONSTRAINT session_client_hint_length CHECK (char_length(client_hint) <= 256),
        origin_hash text NOT NULL
            CONSTRAINT session_origin_hash_format CHECK (origin_hash ~ '{_SHA256_HEX}'),
        status text NOT NULL DEFAULT 'active'
            CONSTRAINT session_status_values
                CHECK (status IN ('active', 'expired', 'closed', 'revoked')),
        end_reason text
            CONSTRAINT session_end_reason_values CHECK (end_reason IN (
                'logout', 'idle_timeout', 'absolute_timeout', 'closed_by_user',
                'password_changed', 'second_factor_reset', 'user_deactivated',
                'organization_suspended'
            )),
        ended_at timestamptz,
        CONSTRAINT session_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT session_absolute_expiry
            CHECK (absolute_expires_at = created_at + interval '12 hours'),
        CONSTRAINT session_seen_after_creation CHECK (last_seen_at >= created_at),
        CONSTRAINT session_end_complete CHECK (
            (status = 'active') = (ended_at IS NULL) AND (ended_at IS NULL) = (end_reason IS NULL)
        )
    )
    """,
    "CREATE INDEX session_by_user ON identity.session (user_id)",
    f"""
    CREATE TABLE identity.invitation (
        invitation_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        user_id uuid NOT NULL,
        token_hash text NOT NULL
            CONSTRAINT invitation_token_hash_format CHECK (token_hash ~ '{_SHA256_HEX}')
            CONSTRAINT invitation_token_hash_unique UNIQUE,
        issued_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'pending'
            CONSTRAINT invitation_status_values
                CHECK (status IN ('pending', 'accepted', 'expired', 'cancelled')),
        invited_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        accepted_at timestamptz,
        disclosed_to_inviter_at timestamptz,
        CONSTRAINT invitation_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT invitation_expiry CHECK (expires_at = issued_at + interval '72 hours'),
        CONSTRAINT invitation_accepted_has_date
            CHECK ((status = 'accepted') = (accepted_at IS NOT NULL))
    )
    """,
    # Una invitación nueva cancela la anterior: a lo sumo una pendiente por usuario (§2.9).
    """
    CREATE UNIQUE INDEX invitation_one_pending_per_user
        ON identity.invitation (user_id) WHERE status = 'pending'
    """,
    """
    CREATE TABLE identity.auth_throttle (
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        subject_kind text NOT NULL
            CONSTRAINT auth_throttle_subject_kind_values
                CHECK (subject_kind IN ('account', 'origin')),
        subject_key text NOT NULL
            CONSTRAINT auth_throttle_subject_key_length
                CHECK (char_length(subject_key) BETWEEN 1 AND 128),
        consecutive_failures integer NOT NULL DEFAULT 0
            CONSTRAINT auth_throttle_failures CHECK (consecutive_failures >= 0),
        window_started_at timestamptz NOT NULL,
        next_allowed_at timestamptz NOT NULL,
        alerted_at timestamptz,
        PRIMARY KEY (organization_id, subject_kind, subject_key)
    )
    """,
    """
    CREATE TABLE identity.provider_concession (
        concession_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        provider_user_id uuid NOT NULL,
        provider_organization_id uuid NOT NULL,
        scope_level text NOT NULL
            CONSTRAINT provider_concession_scope_level_values
                CHECK (scope_level IN ('organization', 'plant')),
        scope_id uuid NOT NULL,
        reason text NOT NULL
            CONSTRAINT provider_concession_reason_length
                CHECK (char_length(reason) BETWEEN 10 AND 500),
        granted_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'active'
            CONSTRAINT provider_concession_status_values
                CHECK (status IN ('active', 'expired', 'revoked')),
        revoked_at timestamptz,
        revoked_by uuid REFERENCES identity.user_account (user_id),
        revoked_by_side text
            CONSTRAINT provider_concession_revoked_by_side_values
                CHECK (revoked_by_side IN ('client', 'provider')),
        CONSTRAINT provider_concession_provider_user_fkey
            FOREIGN KEY (provider_organization_id, provider_user_id)
            REFERENCES identity.user_account (organization_id, user_id),
        CONSTRAINT provider_concession_never_the_provider
            CHECK (organization_id <> provider_organization_id),
        CONSTRAINT provider_concession_organization_scope
            CHECK (scope_level <> 'organization' OR scope_id = organization_id),
        CONSTRAINT provider_concession_duration CHECK (
            expires_at >= granted_at + interval '1 hour'
            AND expires_at <= granted_at + interval '90 days'
        ),
        CONSTRAINT provider_concession_revocation_complete CHECK (
            (status = 'revoked') = (revoked_at IS NOT NULL)
            AND (revoked_at IS NULL) = (revoked_by IS NULL)
            AND (revoked_at IS NULL) = (revoked_by_side IS NULL)
        ),
        CONSTRAINT provider_concession_revoked_after_grant CHECK (revoked_at >= granted_at)
    )
    """,
    """
    CREATE INDEX provider_concession_organization_status
        ON identity.provider_concession (organization_id, status)
    """,
    """
    CREATE TABLE identity.signing_key (
        key_id text PRIMARY KEY
            CONSTRAINT signing_key_key_id_format
                CHECK (key_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'),
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        purpose text NOT NULL
            CONSTRAINT signing_key_purpose_values CHECK (
                purpose IN ('catalog', 'gate', 'live_view_token', 'key_set', 'checkpoint')
            ),
        algorithm text NOT NULL CONSTRAINT signing_key_algorithm CHECK (algorithm = 'Ed25519'),
        public_key text NOT NULL
            CONSTRAINT signing_key_public_key_format CHECK (public_key ~ '^[A-Za-z0-9+/]{43}=$'),
        private_key_ref text NOT NULL
            CONSTRAINT signing_key_private_key_ref_format
                CHECK (char_length(private_key_ref) <= 512
                       AND private_key_ref ~ '^[A-Za-z0-9][A-Za-z0-9:/_+=.@-]*$'),
        valid_from timestamptz NOT NULL,
        valid_until timestamptz,
        status text NOT NULL
            CONSTRAINT signing_key_status_values
                CHECK (status IN ('active', 'overlapping', 'retired')),
        created_at timestamptz NOT NULL,
        rotated_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        CONSTRAINT signing_key_validity CHECK (valid_until > valid_from)
    )
    """,
    # Exactamente una clave active por propósito y a lo sumo una overlapping (§2.12).
    """
    CREATE UNIQUE INDEX signing_key_one_active_per_purpose
        ON identity.signing_key (organization_id, purpose) WHERE status = 'active'
    """,
    """
    CREATE UNIQUE INDEX signing_key_one_overlapping_per_purpose
        ON identity.signing_key (organization_id, purpose) WHERE status = 'overlapping'
    """,
    """
    CREATE TABLE identity.key_set_publication (
        publication_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES identity.organization (organization_id),
        issued_at timestamptz NOT NULL,
        keys jsonb NOT NULL
            CONSTRAINT key_set_publication_keys_array CHECK (jsonb_typeof(keys) = 'array'),
        signed_by_key_id text NOT NULL REFERENCES identity.signing_key (key_id),
        envelope jsonb NOT NULL
            CONSTRAINT key_set_publication_envelope_object
                CHECK (jsonb_typeof(envelope) = 'object')
    )
    """,
    f"""
    CREATE TABLE identity.live_view_token_issuance (
        jti uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        user_id uuid NOT NULL REFERENCES identity.user_account (user_id),
        role_in_use text NOT NULL
            CONSTRAINT live_view_token_issuance_role_values CHECK (role_in_use IN ({_ROLES})),
        issued_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        correlation_id uuid NOT NULL,
        CONSTRAINT live_view_token_issuance_zone_fkey
            FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT live_view_token_issuance_node_fkey
            FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT live_view_token_issuance_expiry
            CHECK (expires_at = issued_at + interval '600 seconds')
    )
    """,
    """
    CREATE INDEX live_view_token_issuance_zone
        ON identity.live_view_token_issuance (zone_id, issued_at)
    """,
    """
    CREATE TABLE identity.privacy_notice_acceptance (
        acceptance_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        user_id uuid NOT NULL,
        notice_version text NOT NULL
            CONSTRAINT privacy_notice_acceptance_version_length
                CHECK (char_length(notice_version) BETWEEN 1 AND 32),
        accepted_at timestamptz NOT NULL,
        correlation_id uuid NOT NULL,
        CONSTRAINT privacy_notice_acceptance_user_fkey FOREIGN KEY (organization_id, user_id)
            REFERENCES identity.user_account (organization_id, user_id)
    )
    """,
)

TENANT_TABLES = (
    "organization",
    "user_account",
    "plant",
    "zone",
    "node_identity",
    "zone_node_assignment",
    "role_assignment",
    "password_credential",
    "totp_credential",
    "recovery_code",
    "session",
    "invitation",
    "auth_throttle",
    "provider_concession",
    "signing_key",
    "key_set_publication",
    "live_view_token_issuance",
    "privacy_notice_acceptance",
)
"""Las 18 tablas con seguridad a nivel de fila forzada por organización (BR-NUC-01)."""

PROVIDER_SCOPED_TABLES = (
    "plant",
    "zone",
    "node_identity",
    "zone_node_assignment",
    "live_view_token_issuance",
)
"""Tablas con ``plant_id`` o ``zone_id``: llevan además la política de proveedor (PR-NUC-52)."""

APPEND_ONLY_GUARDS = {
    "zone_node_assignment": "identity.guard_zone_node_assignment()",
    "role_assignment": "identity.guard_role_assignment()",
    "provider_concession": "identity.guard_provider_concession()",
    "key_set_publication": "identity.reject_append_only_change()",
    "live_view_token_issuance": "identity.reject_append_only_change()",
    "privacy_notice_acceptance": "identity.reject_append_only_change()",
}
"""Tablas ⛓ y la función que custodia su ``UPDATE`` y ``DELETE``."""

_APP_UPDATABLE_COLUMNS = {
    "organization": "name, status, concession_max_days, concession_default_days",
    "user_account": (
        "display_name, professional_license, status, second_factor_required, "
        "second_factor_enrolled_at, password_updated_at, last_login_at, deactivated_at, "
        "privacy_notice_version_accepted"
    ),
    "plant": "name, timezone, status",
    "zone": "name",
    "node_identity": "status, live_view_local_url",
    "password_credential": "password_hash, algorithm_version, updated_at, breach_checked_at",
    "totp_credential": (
        "secret_encrypted, data_key_wrapped, enrolled_at, last_accepted_step, disabled_at"
    ),
    "recovery_code": "used_at",
    "session": (
        "last_seen_at, idle_expires_at, second_factor_verified, status, end_reason, ended_at"
    ),
    "invitation": "status, accepted_at, disclosed_to_inviter_at",
    "auth_throttle": "consecutive_failures, window_started_at, next_allowed_at, alerted_at",
    "signing_key": "valid_until, status",
    # Solo anexar: únicamente las columnas de la actualización de cierre.
    "zone_node_assignment": "unassigned_at",
    "role_assignment": "removed_at, removed_by",
    "provider_concession": "status, revoked_at, revoked_by, revoked_by_side",
}
"""Columnas que ``vigia_app`` puede actualizar; las demás tablas, ninguna."""

_ORGANIZATION_POLICY = """
CREATE POLICY organization_isolation ON identity.{table} AS PERMISSIVE FOR ALL TO PUBLIC
    USING (organization_id = identity.rls_organization_id())
    WITH CHECK (organization_id = identity.rls_organization_id())
"""

_PROVIDER_POLICY = """
CREATE POLICY provider_concession_scope ON identity.{table} AS RESTRICTIVE FOR ALL TO PUBLIC
    USING (identity.rls_provider_scope_allows(organization_id, plant_id))
    WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))
"""


def _row_security() -> list[str]:
    statements: list[str] = []
    for table in TENANT_TABLES:
        statements += [
            f"ALTER TABLE identity.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE identity.{table} FORCE ROW LEVEL SECURITY",
            _ORGANIZATION_POLICY.format(table=table),
        ]
        if table in PROVIDER_SCOPED_TABLES:
            statements.append(_PROVIDER_POLICY.format(table=table))
    return statements


def _triggers() -> list[str]:
    statements: list[str] = []
    for table, function in APPEND_ONLY_GUARDS.items():
        statements += [
            f"CREATE TRIGGER append_only_guard BEFORE UPDATE OR DELETE ON identity.{table}"
            f" FOR EACH ROW EXECUTE FUNCTION {function}",
            f"CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON identity.{table}"
            " FOR EACH STATEMENT EXECUTE FUNCTION identity.reject_append_only_change()",
        ]
    statements += [
        "CREATE TRIGGER data_region_immutable BEFORE UPDATE OF data_region ON identity.plant"
        " FOR EACH ROW EXECUTE FUNCTION identity.reject_data_region_change()",
        "CREATE TRIGGER role_organization_kind BEFORE INSERT ON identity.role_assignment"
        " FOR EACH ROW EXECUTE FUNCTION identity.check_role_organization_kind()",
        "CREATE TRIGGER concession_on_client BEFORE INSERT ON identity.provider_concession"
        " FOR EACH ROW EXECUTE FUNCTION identity.check_concession_client()",
    ]
    return statements


def _grants() -> list[str]:
    statements = [
        "REVOKE ALL ON FUNCTION identity.rls_organization_id() FROM PUBLIC",
        "REVOKE ALL ON FUNCTION identity.rls_provider_scope_allows(uuid, uuid) FROM PUBLIC",
        "GRANT EXECUTE ON FUNCTION identity.rls_organization_id() TO vigia_app",
        "GRANT EXECUTE ON FUNCTION identity.rls_provider_scope_allows(uuid, uuid) TO vigia_app",
    ]
    for function in (
        "reject_append_only_change",
        "guard_zone_node_assignment",
        "guard_role_assignment",
        "guard_provider_concession",
        "reject_data_region_change",
        "check_role_organization_kind",
        "check_concession_client",
    ):
        statements.append(f"REVOKE ALL ON FUNCTION identity.{function}() FROM PUBLIC")
    for table in TENANT_TABLES:
        statements.append(f"GRANT SELECT, INSERT ON identity.{table} TO vigia_app")
        columns = _APP_UPDATABLE_COLUMNS.get(table)
        if columns is not None:
            statements.append(f"GRANT UPDATE ({columns}) ON identity.{table} TO vigia_app")
    return statements


def _comments() -> list[str]:
    return [
        f"COMMENT ON TABLE identity.{table} IS"
        f" '{entity} (domain-entities §2){' ⛓ solo anexar' if table in APPEND_ONLY_GUARDS else ''}"
        "; seguridad a nivel de fila forzada por organización'"
        for table, entity in zip(TENANT_TABLES, _ENTITIES, strict=True)
    ]


_ENTITIES = (
    "Organization",
    "User",
    "Plant",
    "Zone",
    "NodeIdentity",
    "ZoneNodeAssignment",
    "RoleAssignment",
    "PasswordCredential",
    "TotpCredential",
    "RecoveryCode",
    "Session",
    "Invitation",
    "AuthThrottle",
    "ProviderConcession",
    "SigningKey",
    "KeySetPublication",
    "LiveViewTokenIssuance",
    "PrivacyNoticeAcceptance",
)


def upgrade() -> None:
    # Todo lo que se crea es de vigia_migrate, lo ejecute el maestro (primer despliegue) o él.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_TABLES, *_FUNCTIONS, *_row_security(), *_triggers(), *_grants()):
        op.execute(statement)
    for statement in _comments():
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
