"""Concesión vigente en la sentencia única del contexto (TASK-125, LC-NUC-04; PAT-NUC-REN-03).

Revisión nuc_0008. ``build_context(session)`` es **una** sentencia que corre con la seguridad a
nivel de fila de la organización de la sesión. Bajo concesión (``X-Vigia-Concession``) esa
organización es la **proveedora**, y la fila de ``identity.provider_concession`` es del
**cliente**: la política ``organization_isolation`` la oculta. Sin esta función el contexto del
proveedor necesitaría una segunda transacción con otra organización fijada.

- ``identity.session_concession(concession_id, provider_user_id, at)``: función
  ``SECURITY DEFINER`` de ``vigia_migrate`` con ``search_path`` fijo que devuelve, a lo sumo, la
  fila **de esa concesión** con ``organization_id`` del cliente, alcance y vencimiento, y solo si:
  la transacción que llama tiene fijada la organización **proveedora** de la concesión
  (``provider_organization_id = vigia.organization_id``), la concesión es de ``provider_user_id``,
  está ``active`` sin revocar, ``granted_at <= at < expires_at`` (BR-NUC-40: sin depender de la
  tarea ``expire_concessions``) y el cliente está activo. Nunca devuelve el motivo ni nada de otra
  concesión, y desde el contexto de un cliente no devuelve nada.
- Las políticas ``concession_lookup`` dejan a ``vigia_migrate`` leer ``provider_concession`` y
  ``organization`` **solo** mientras ``vigia.concession_lookup`` vale ``on``, y eso ocurre
  únicamente dentro de la función (la fija al entrar y la vacía antes de salir), como
  ``login_lookup`` de ``nuc_0007``. ``vigia_app`` solo puede ejecutarla; fijar él mismo la
  variable no le da nada, porque las políticas son solo para ``vigia_migrate``.

El endurecimiento de la RLS de ``identity`` para contextos de proveedor (adenda A-46) es de
TASK-127 y no cambia esta función: se ejecuta con el contexto de la proveedora, sin concesión.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0008"
down_revision: str | None = "nuc_0007"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    CREATE POLICY concession_lookup ON identity.provider_concession
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING (pg_catalog.current_setting('vigia.concession_lookup', true) = 'on')
    """,
    """
    CREATE POLICY concession_lookup ON identity.organization
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING (pg_catalog.current_setting('vigia.concession_lookup', true) = 'on')
    """,
    # Como en nuc_0007: la variable se fija con set_config dentro y se vacía antes de salir.
    """
    CREATE FUNCTION identity.session_concession(
        selected uuid, provider_user uuid, at timestamptz
    )
        RETURNS TABLE (
            concession_id uuid,
            organization_id uuid,
            scope_level text,
            scope_id uuid,
            expires_at timestamptz
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        caller uuid := shared.vigia_current_organization();
    BEGIN
        IF selected IS NULL OR provider_user IS NULL OR at IS NULL OR caller IS NULL THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', 'on', true);
        RETURN QUERY
            SELECT concession.concession_id, concession.organization_id,
                concession.scope_level, concession.scope_id, concession.expires_at
            FROM identity.provider_concession AS concession
            JOIN identity.organization AS client
                ON client.organization_id = concession.organization_id
            WHERE concession.concession_id = selected
                AND concession.provider_user_id = provider_user
                AND concession.provider_organization_id = caller
                AND concession.status = 'active'
                AND concession.revoked_at IS NULL
                AND concession.granted_at <= at
                AND concession.expires_at > at
                AND client.kind = 'client'
                AND client.status = 'active';
        PERFORM pg_catalog.set_config('vigia.concession_lookup', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION identity.session_concession(uuid, uuid, timestamptz) IS
        'Concesión vigente del usuario del proveedor, vista desde el contexto de la proveedora'
    """,
    """
    COMMENT ON POLICY concession_lookup ON identity.provider_concession IS
        'Solo dentro de identity.session_concession (vigia.concession_lookup = on)'
    """,
    """
    COMMENT ON POLICY concession_lookup ON identity.organization IS
        'Solo dentro de identity.session_concession (vigia.concession_lookup = on)'
    """,
    "REVOKE ALL ON FUNCTION identity.session_concession(uuid, uuid, timestamptz) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.session_concession(uuid, uuid, timestamptz) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0004 y nuc_0007: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
