"""Concesiones propias del usuario del proveedor (TASK-136, LC-NUC-06; BR-NUC-35, 39).

Revisión nuc_0014. ``GET /provider/concessions`` (``business-logic-model.md`` §10.2, lado
proveedor) lista las concesiones que un usuario de la proveedora se concedió, para elegir la que
selecciona con ``X-Vigia-Concession`` y para ver su estado. Desde el contexto de la proveedora la
seguridad a nivel de fila no deja ver ninguna fila de ``identity.provider_concession`` (son del
cliente, ``nuc_0004`` y ``nuc_0009``), igual que ``identity.provider_concession_of`` de
``nuc_0009`` resuelve una sola concesión por su identificador.

``identity.provider_concessions_of(grantee)`` es la misma búsqueda para una lista: función
``SECURITY DEFINER`` de ``vigia_migrate`` con ``search_path`` fijo que devuelve, **sin el
motivo**, las concesiones de la proveedora que llama (``vigia.organization_id``) concedidas a
``grantee``, de la más reciente a la más antigua. Un contexto de proveedor bajo concesión (o un
contexto de un cliente: ``provider_organization_id`` nunca es un cliente) no obtiene nada. Como
en ``nuc_0009``, ``vigia.concession_lookup`` vale ``on`` solo dentro de la función; ``vigia_app``
solo puede ejecutarla. Quién es ``grantee`` lo decide el servicio: el actor de la sesión.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0014"
down_revision: str | None = "nuc_0013"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    CREATE FUNCTION identity.provider_concessions_of(grantee uuid)
        RETURNS TABLE (
            concession_id uuid,
            organization_id uuid,
            provider_user_id uuid,
            scope_level text,
            scope_id uuid,
            granted_at timestamptz,
            expires_at timestamptz,
            status text,
            revoked_at timestamptz,
            revoked_by_side text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        caller uuid := shared.vigia_current_organization();
    BEGIN
        IF grantee IS NULL OR caller IS NULL OR identity.rls_provider_context() THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', 'on', true);
        RETURN QUERY
            SELECT concession.concession_id, concession.organization_id,
                concession.provider_user_id, concession.scope_level, concession.scope_id,
                concession.granted_at, concession.expires_at, concession.status,
                concession.revoked_at, concession.revoked_by_side
            FROM identity.provider_concession AS concession
            WHERE concession.provider_organization_id = caller
                AND concession.provider_user_id = grantee
            ORDER BY concession.granted_at DESC, concession.concession_id DESC;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION identity.provider_concessions_of(uuid) IS
        'Concesiones de la proveedora que llama concedidas a grantee (sin el motivo)'
    """,
    "REVOKE ALL ON FUNCTION identity.provider_concessions_of(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.provider_concessions_of(uuid) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0009: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
