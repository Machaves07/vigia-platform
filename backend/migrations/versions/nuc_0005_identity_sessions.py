"""Sesiones e inicio de sesión (TASK-124, LC-NUC-03; BR-NUC-22 a 27; PAT-NUC-SEG-03).

Revisión nuc_0005. Completa el esquema de ``nuc_0004`` con lo que necesita el inicio de sesión:

- ``session_idle_expiry``: ``idle_expires_at = last_seen_at + 30 min`` (``domain-entities.md``
  §2.8). ``nuc_0004`` ya exigía las 12 h absolutas; sin esta restricción una sesión podía
  insertarse o prolongarse con cualquier vencimiento por inactividad.
- ``identity.login_organization(email)``: la única búsqueda que ocurre **antes** de conocer la
  organización (el correo es único en toda la plataforma, BR-NUC-05). Con ``FORCE ROW LEVEL
  SECURITY`` y sin ``vigia.organization_id`` no se ve ningún usuario, así que es una función
  ``SECURITY DEFINER`` de ``vigia_migrate`` con ``search_path`` fijo que **solo devuelve el
  ``organization_id``** de ese correo (o nulo). ``FORCE`` también afecta al dueño: la política
  ``login_lookup`` le deja leer ``user_account`` solo mientras ``vigia.login_lookup`` vale ``on``,
  y eso ocurre únicamente dentro de la función (la fija al entrar y la vacía antes de salir).
  ``vigia_app`` solo puede ejecutarla; fijar él mismo la variable no le da nada, porque la
  política es solo para ``vigia_migrate`` y ``vigia_app`` no pertenece a ese rol (``nuc_0001``).

La sesión no necesita una búsqueda así: la cookie lleva la organización junto al identificador
(``identity.auth.sessions``), y una organización falsa no encuentra la fila por la seguridad a
nivel de fila.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0005"
down_revision: str | None = "nuc_0004"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE identity.session
        ADD CONSTRAINT session_idle_expiry
            CHECK (idle_expires_at = last_seen_at + interval '30 minutes')
    """,
    """
    CREATE POLICY login_lookup ON identity.user_account AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING (pg_catalog.current_setting('vigia.login_lookup', true) = 'on')
    """,
    # PostgreSQL 15+ no deja a un rol sin SUPERUSER poner una variable propia en la cláusula
    # SET de la función: se fija con set_config dentro y se vacía antes de salir.
    """
    CREATE FUNCTION identity.login_organization(normalized_email text) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        found uuid;
    BEGIN
        PERFORM pg_catalog.set_config('vigia.login_lookup', 'on', true);
        SELECT account.organization_id INTO found
        FROM identity.user_account AS account
        WHERE account.email = normalized_email;
        PERFORM pg_catalog.set_config('vigia.login_lookup', '', true);
        RETURN found;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION identity.login_organization(text) IS
        'Organización del correo (o nulo): la búsqueda previa al contexto del inicio de sesión'
    """,
    """
    COMMENT ON POLICY login_lookup ON identity.user_account IS
        'Solo dentro de identity.login_organization (vigia.login_lookup = on en su definición)'
    """,
    "REVOKE ALL ON FUNCTION identity.login_organization(text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.login_organization(text) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0004: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
