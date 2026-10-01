"""Aislamiento del proveedor en toda la base de ``identity`` y reglas de la concesión (TASK-127).

Revisión nuc_0009 (LC-NUC-06; adenda A-46, BR-NUC-04, 35, 39, 40, 42; PR-NUC-52). Hasta aquí la
RLS de ``identity`` solo acotaba un contexto de proveedor (``vigia.actor_kind`` del proveedor o
``vigia.concession_id`` presente) en las cinco tablas con planta o zona (``nuc_0004``). En las
otras trece, ese contexto veía secretos de autenticación, usuarios, roles y concesiones del
cliente aunque la concesión estuviera vencida o revocada. La aplicación (TASK-125) ya lo impedía;
ahora la base también.

**Políticas RESTRICTIVE nuevas** (se suman con ``AND`` a ``organization_isolation``):

- ``provider_context_denied`` en los secretos y credenciales (``password_credential``,
  ``totp_credential``, ``recovery_code``, ``session``, ``invitation``, ``auth_throttle``,
  ``signing_key``, ``key_set_publication``): con un contexto de proveedor, ninguna fila visible
  ni escribible.
- ``provider_concession_read`` (solo ``SELECT``) en ``organization``, ``user_account``,
  ``role_assignment`` y ``privacy_notice_acceptance``: con un contexto de proveedor, solo con la
  concesión de ``vigia.concession_id`` vigente (``active``, sin revocar, ``granted_at <= now() <
  expires_at``) y dentro de su alcance: la fila de la organización con cualquier alcance; usuarios
  y aceptaciones del aviso, solo con alcance de organización; una asignación de rol, si su planta
  (la suya o la de su zona) está en el alcance. ``provider_context_read_only`` (``INSERT``,
  ``UPDATE``, ``DELETE``): ninguna escritura desde un contexto de proveedor.
- En ``provider_concession``:
  * ``provider_concession_own`` (``SELECT``): un contexto de proveedor solo ve **su** concesión
    vigente, o la fila que él mismo revocó (PostgreSQL comprueba la fila nueva del ``UPDATE`` de
    cierre contra esta política); vencida o revocada por el cliente, nada;
  * ``provider_concession_grant`` (``INSERT``): una concesión solo nace desde el contexto de
    concesión de **esa misma fila** (``vigia.actor_kind = provider_user``, ``vigia.concession_id``
    igual a la fila), activa y sin vencer. Ni un contexto de cliente ni el de otra concesión
    crean o amplían concesiones (BR-NUC-35, A-46);
  * ``provider_concession_close`` (``UPDATE``): un contexto de proveedor solo cierra su concesión
    vigente como ``revoked`` con ``revoked_by_side = provider``; ningún otro contexto escribe
    ``revoked_by_side = provider`` (el ``CHECK`` de ``nuc_0004`` exige además el lado al revocar).
    El lado de la revocación dice la verdad (P5).

**Restricciones entre filas** que el ``CHECK`` de ``nuc_0004`` no podía imponer (revisión de
VIG-38): ``identity.check_concession_client()`` se sustituye por una función ``SECURITY DEFINER``
que, al dar de alta, exige cliente de tipo ``client`` y activo, ``provider_organization_id`` de
tipo ``provider``, la planta del alcance dentro del cliente y ``expires_at - granted_at`` dentro de
``concession_max_days`` **del cliente** (no los 90 días fijos de ``nuc_0004``). La guarda de
cierre ``identity.guard_provider_concession()`` exige además ``revoked_at < expires_at`` (no se
revoca lo vencido) y ``expires_at <= now()`` para cerrar como ``expired`` (no se vence antes de
tiempo). Cada rechazo lleva su restricción con nombre para que el servicio lo traduzca.

**Consultas desde la proveedora** (``SECURITY DEFINER``, solo ``EXECUTE`` para ``vigia_app``):

- ``identity.concession_terms(client)``: ``concession_max_days`` y ``concession_default_days`` de
  un cliente activo; solo desde el contexto de la proveedora sin concesión. Nada más del cliente.
- ``identity.provider_concession_of(concession)``: la concesión de esa proveedora, sin el motivo,
  para que el concesionario o un operador la revoquen desde su propia organización.

Las funciones de búsqueda fijan ``vigia.concession_lookup`` como las de ``nuc_0007`` y ``nuc_0008``;
mientras vale ``on`` y el usuario efectivo es ``vigia_migrate`` (solo dentro de ellas),
``identity.rls_provider_context()`` no cuenta como contexto de proveedor, y la nueva política
``concession_lookup`` de ``plant`` deja leer la planta del alcance. ``vigia_app`` no gana nada
fijando la variable: el usuario efectivo sigue siendo él.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0009"
down_revision: str | None = "nuc_0008"
branch_labels: None = None
depends_on: None = None

SECRET_TABLES = (
    "password_credential",
    "totp_credential",
    "recovery_code",
    "session",
    "invitation",
    "auth_throttle",
    "signing_key",
    "key_set_publication",
)
"""Secretos y credenciales: sin filas con un contexto de proveedor (A-46, punto 1)."""

_READ_ONLY_REACH = {
    "organization": "identity.rls_concession_reaches(organization_id, NULL, true)",
    "user_account": "identity.rls_concession_reaches(organization_id, NULL, false)",
    "privacy_notice_acceptance": "identity.rls_concession_reaches(organization_id, NULL, false)",
    "role_assignment": (
        "identity.rls_concession_reaches(organization_id, CASE scope_level"
        " WHEN 'plant' THEN scope_id"
        " WHEN 'zone' THEN (SELECT zone.plant_id FROM identity.zone AS zone"
        " WHERE zone.zone_id = role_assignment.scope_id) END, false)"
    ),
}
"""Lo que un contexto de proveedor puede leer de cada tabla: su concesión vigente y su alcance."""

_FUNCTIONS = (
    # ¿Es un contexto de proveedor? Fallo cerrado: basta una de las dos variables. Dentro de una
    # función de búsqueda de vigia_migrate (vigia.concession_lookup = on) no cuenta.
    """
    CREATE FUNCTION identity.rls_provider_context() RETURNS boolean
        LANGUAGE sql
        STABLE
    AS $$
        SELECT (
                COALESCE(pg_catalog.current_setting('vigia.actor_kind', true), '')
                    IN ('provider', 'provider_user')
                OR COALESCE(pg_catalog.current_setting('vigia.concession_id', true), '') <> ''
            )
            AND NOT (
                CURRENT_USER = 'vigia_migrate'
                AND COALESCE(pg_catalog.current_setting('vigia.concession_lookup', true), '')
                    = 'on'
            )
    $$
    """,
    # ¿Alcanza la concesión vigente del contexto a una fila de esta organización y planta?
    # any_scope: basta cualquier alcance (la fila de la organización); si no, alcance de
    # organización o la planta de la fila (sin planta, solo alcance de organización).
    """
    CREATE FUNCTION identity.rls_concession_reaches(
        row_organization_id uuid, row_plant_id uuid, any_scope boolean
    ) RETURNS boolean
        LANGUAGE sql
        STABLE
    AS $$
        SELECT EXISTS (
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
                    any_scope
                    OR concession.scope_level = 'organization'
                    OR (concession.scope_level = 'plant' AND concession.scope_id = row_plant_id)
                )
        )
    $$
    """,
    # nuc_0004 con la salida de las funciones de búsqueda: el mismo criterio que la anterior.
    """
    CREATE OR REPLACE FUNCTION identity.rls_provider_scope_allows(
        row_organization_id uuid, row_plant_id uuid
    )
        RETURNS boolean
        LANGUAGE sql
        STABLE
    AS $$
        SELECT NOT identity.rls_provider_context()
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
    # Alta de una concesión: las reglas entre filas (BR-NUC-35, 42). SECURITY DEFINER para leer
    # la proveedora y la planta del alcance, que el contexto que inserta no ve.
    """
    CREATE OR REPLACE FUNCTION identity.check_concession_client() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        client_kind text;
        client_status text;
        client_max_days integer;
        provider_kind text;
        plant_found boolean := true;
    BEGIN
        PERFORM pg_catalog.set_config('vigia.concession_lookup', 'on', true);
        SELECT kind, status, concession_max_days
            INTO client_kind, client_status, client_max_days
            FROM identity.organization WHERE organization_id = NEW.organization_id;
        SELECT kind INTO provider_kind
            FROM identity.organization WHERE organization_id = NEW.provider_organization_id;
        IF NEW.scope_level = 'plant' THEN
            plant_found := EXISTS (
                SELECT FROM identity.plant
                WHERE plant_id = NEW.scope_id AND organization_id = NEW.organization_id
            );
        END IF;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', '', true);
        IF client_kind IS DISTINCT FROM 'client' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'provider_concession_client_kind',
                MESSAGE = 'una concesión solo se otorga sobre una organización cliente visible';
        END IF;
        IF provider_kind IS DISTINCT FROM 'provider' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'provider_concession_provider_kind',
                MESSAGE = 'la concesión solo la recibe un usuario de la organización proveedora';
        END IF;
        IF client_status IS DISTINCT FROM 'active' THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'provider_concession_client_active',
                MESSAGE = 'no se concede acceso sobre una organización cliente suspendida';
        END IF;
        IF NOT plant_found THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'provider_concession_scope_plant',
                MESSAGE = 'la planta del alcance no es de la organización cliente';
        END IF;
        IF NEW.expires_at > NEW.granted_at + pg_catalog.make_interval(days => client_max_days) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'provider_concession_max_days',
                MESSAGE = 'la duración supera concession_max_days del cliente';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    # Cierre: una sola vez, de active a revoked (antes de vencer) o a expired (ya vencida).
    """
    CREATE OR REPLACE FUNCTION identity.guard_provider_concession() RETURNS trigger
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
            IF NEW.status = 'revoked' AND NOT NEW.revoked_at < OLD.expires_at THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    CONSTRAINT = 'provider_concession_revoked_before_expiry',
                    MESSAGE = 'una concesión vencida no se revoca: vence';
            END IF;
            IF NEW.status = 'expired' AND OLD.expires_at > pg_catalog.now() THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    CONSTRAINT = 'provider_concession_expired_after_expiry',
                    MESSAGE = 'una concesión no vence antes de expires_at';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = format('identity.provider_concession es de solo anexar: %s está prohibido'
                             ' salvo cerrar una vez una concesión activa (P4)', TG_OP);
    END
    $$
    """,
    # Términos del cliente para conceder, vistos desde la proveedora sin concesión.
    """
    CREATE FUNCTION identity.concession_terms(client uuid)
        RETURNS TABLE (concession_max_days integer, concession_default_days integer)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        caller uuid := shared.vigia_current_organization();
    BEGIN
        IF client IS NULL OR caller IS NULL OR identity.rls_provider_context() THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', 'on', true);
        RETURN QUERY
            SELECT target.concession_max_days, target.concession_default_days
            FROM identity.organization AS target
            JOIN identity.organization AS provider
                ON provider.organization_id = caller
                AND provider.kind = 'provider'
                AND provider.status = 'active'
            WHERE target.organization_id = client
                AND target.kind = 'client'
                AND target.status = 'active';
        PERFORM pg_catalog.set_config('vigia.concession_lookup', '', true);
    END
    $$
    """,
    # Una concesión de esta proveedora (sin el motivo), para revocarla desde la proveedora.
    """
    CREATE FUNCTION identity.provider_concession_of(selected uuid)
        RETURNS TABLE (
            concession_id uuid,
            organization_id uuid,
            provider_user_id uuid,
            scope_level text,
            scope_id uuid,
            granted_at timestamptz,
            expires_at timestamptz,
            status text,
            revoked_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        caller uuid := shared.vigia_current_organization();
    BEGIN
        IF selected IS NULL OR caller IS NULL OR identity.rls_provider_context() THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', 'on', true);
        RETURN QUERY
            SELECT concession.concession_id, concession.organization_id,
                concession.provider_user_id, concession.scope_level, concession.scope_id,
                concession.granted_at, concession.expires_at, concession.status,
                concession.revoked_at
            FROM identity.provider_concession AS concession
            WHERE concession.concession_id = selected
                AND concession.provider_organization_id = caller;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', '', true);
    END
    $$
    """,
)

_IN_FORCE = (
    "concession_id = NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid"
    " AND status = 'active' AND revoked_at IS NULL"
    " AND granted_at <= pg_catalog.now() AND expires_at > pg_catalog.now()"
)
"""La fila es la concesión del contexto y está vigente (BR-NUC-40)."""

_CONCESSION_POLICIES = (
    # PostgreSQL comprueba también la fila nueva de un UPDATE con WHERE contra la política de
    # SELECT: la revocación del propio proveedor tiene que poder ver la fila que acaba de cerrar.
    f"""
    CREATE POLICY provider_concession_own ON identity.provider_concession
        AS RESTRICTIVE FOR SELECT TO PUBLIC
        USING (
            NOT identity.rls_provider_context()
            OR ({_IN_FORCE})
            OR (
                concession_id
                    = NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid
                AND status = 'revoked'
                AND revoked_by_side = 'provider'
            )
        )
    """,
    """
    CREATE POLICY provider_concession_grant ON identity.provider_concession
        AS RESTRICTIVE FOR INSERT TO PUBLIC
        WITH CHECK (
            COALESCE(pg_catalog.current_setting('vigia.actor_kind', true), '') = 'provider_user'
            AND concession_id
                = NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid
            AND status = 'active'
            AND revoked_at IS NULL
            AND expires_at > pg_catalog.now()
        )
    """,
    f"""
    CREATE POLICY provider_concession_close ON identity.provider_concession
        AS RESTRICTIVE FOR UPDATE TO PUBLIC
        USING (NOT identity.rls_provider_context() OR ({_IN_FORCE}))
        WITH CHECK (
            CASE WHEN identity.rls_provider_context()
                THEN concession_id
                        = NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid
                    AND status = 'revoked'
                    AND revoked_by_side = 'provider'
                ELSE revoked_by_side IS DISTINCT FROM 'provider'
            END
        )
    """,
)

_LOOKUP_POLICY = """
CREATE POLICY concession_lookup ON identity.plant
    AS PERMISSIVE FOR SELECT TO vigia_migrate
    USING (pg_catalog.current_setting('vigia.concession_lookup', true) = 'on')
"""


def _secret_policies() -> list[str]:
    return [
        f"CREATE POLICY provider_context_denied ON identity.{table}"
        " AS RESTRICTIVE FOR ALL TO PUBLIC"
        " USING (NOT identity.rls_provider_context())"
        " WITH CHECK (NOT identity.rls_provider_context())"
        for table in SECRET_TABLES
    ]


def _read_only_policies() -> list[str]:
    statements: list[str] = []
    for table, reach in _READ_ONLY_REACH.items():
        statements += [
            f"CREATE POLICY provider_concession_read ON identity.{table}"
            " AS RESTRICTIVE FOR SELECT TO PUBLIC"
            f" USING (NOT identity.rls_provider_context() OR {reach})",
            f"CREATE POLICY provider_context_read_only ON identity.{table}"
            " AS RESTRICTIVE FOR INSERT TO PUBLIC"
            " WITH CHECK (NOT identity.rls_provider_context())",
            f"CREATE POLICY provider_context_no_update ON identity.{table}"
            " AS RESTRICTIVE FOR UPDATE TO PUBLIC"
            " USING (NOT identity.rls_provider_context())"
            " WITH CHECK (NOT identity.rls_provider_context())",
            f"CREATE POLICY provider_context_no_delete ON identity.{table}"
            " AS RESTRICTIVE FOR DELETE TO PUBLIC"
            " USING (NOT identity.rls_provider_context())",
        ]
    return statements


_GRANTS = (
    "REVOKE ALL ON FUNCTION identity.rls_provider_context() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.rls_provider_context() TO vigia_app",
    "REVOKE ALL ON FUNCTION identity.rls_concession_reaches(uuid, uuid, boolean) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.rls_concession_reaches(uuid, uuid, boolean) TO vigia_app",
    "REVOKE ALL ON FUNCTION identity.concession_terms(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.concession_terms(uuid) TO vigia_app",
    "REVOKE ALL ON FUNCTION identity.provider_concession_of(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.provider_concession_of(uuid) TO vigia_app",
)

_COMMENTS = (
    """
    COMMENT ON FUNCTION identity.rls_provider_context() IS
        'Contexto de proveedor (actor del proveedor o vigia.concession_id); no cuenta dentro de'
        ' una función de búsqueda de vigia_migrate'
    """,
    """
    COMMENT ON FUNCTION identity.concession_terms(uuid) IS
        'Tope y duración por omisión de un cliente activo, vistos desde la proveedora sin concesión'
    """,
    """
    COMMENT ON FUNCTION identity.provider_concession_of(uuid) IS
        'Una concesión de la proveedora que llama (sin el motivo), para revocarla desde ella'
    """,
    """
    COMMENT ON POLICY concession_lookup ON identity.plant IS
        'Solo dentro de identity.check_concession_client (vigia.concession_lookup = on)'
    """,
)


def upgrade() -> None:
    # Como en nuc_0004, nuc_0007 y nuc_0008: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (
        *_FUNCTIONS,
        *_secret_policies(),
        *_read_only_policies(),
        *_CONCESSION_POLICIES,
        _LOOKUP_POLICY,
        *_GRANTS,
        *_COMMENTS,
    ):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
