"""Conteo del límite de vista en vivo por usuario, sin la RLS del contexto (TASK-128, LC-NUC-26).

Revisión nuc_0012 (BR-NUC-90, PAT-NUC-ESC-03, PR-NUC-45). ``emitir_token_vista`` cuenta las
emisiones del usuario en los últimos 10 minutos dentro de la transacción del contexto autorizado.
Con la seguridad a nivel de fila de ``identity.live_view_token_issuance``
(``organization_isolation`` y ``provider_concession_scope``), ese conteo solo veía las emisiones
de la organización y de la concesión en uso: un usuario del proveedor con varias concesiones (otra
planta u otra organización) tenía un límite de 30 por cada una. El límite es **por usuario**.

- ``identity.live_view_issuances_since(subject, since)``: función ``SECURITY DEFINER`` de
  ``vigia_migrate`` con ``search_path`` fijo que devuelve **solo** el número de emisiones de
  ``subject`` con ``issued_at > since`` en todas las organizaciones y la más antigua de ellas.
  Ninguna otra columna ni fila. Exige una organización fijada en la transacción que llama (fallo
  cerrado: sin ella, error, no cero).
- La política ``live_view_rate_lookup`` deja a ``vigia_migrate`` leer las emisiones **solo**
  mientras ``vigia.concession_lookup`` vale ``on``, y eso ocurre únicamente dentro de la función
  (la fija al entrar y la vacía antes de salir), como las búsquedas de ``nuc_0007`` a ``nuc_0011``.
  Con esa variable, ``identity.rls_provider_context()`` no cuenta como contexto de proveedor, así
  que ``provider_concession_scope`` deja pasar las filas. ``vigia_app`` solo puede ejecutarla;
  fijar él mismo la variable no le da nada, porque la política es solo para ``vigia_migrate``.

La exclusión sigue siendo el candado consultivo por usuario de ``shared.tokens``, que ya era
global (no depende de la organización).
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0012"
down_revision: str | None = "nuc_0011"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    CREATE POLICY live_view_rate_lookup ON identity.live_view_token_issuance
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING (pg_catalog.current_setting('vigia.concession_lookup', true) = 'on')
    """,
    # Como en nuc_0008: la variable se fija con set_config dentro y se vacía antes de salir.
    """
    CREATE FUNCTION identity.live_view_issuances_since(subject uuid, since timestamptz)
        RETURNS TABLE (issued bigint, oldest timestamptz)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF subject IS NULL OR since IS NULL OR shared.vigia_current_organization() IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'invalid_parameter_value',
                MESSAGE = 'live_view_issuances_since necesita usuario, inicio y organización';
        END IF;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', 'on', true);
        RETURN QUERY
            SELECT pg_catalog.count(*), pg_catalog.min(issuance.issued_at)
            FROM identity.live_view_token_issuance AS issuance
            WHERE issuance.user_id = subject AND issuance.issued_at > since;
        PERFORM pg_catalog.set_config('vigia.concession_lookup', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION identity.live_view_issuances_since(uuid, timestamptz) IS
        'Emisiones de vista en vivo del usuario desde un instante, en todas las organizaciones'
        ' (solo número y la más antigua; BR-NUC-90)'
    """,
    """
    COMMENT ON POLICY live_view_rate_lookup ON identity.live_view_token_issuance IS
        'Solo dentro de identity.live_view_issuances_since (vigia.concession_lookup = on)'
    """,
    "REVOKE ALL ON FUNCTION identity.live_view_issuances_since(uuid, timestamptz) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.live_view_issuances_since(uuid, timestamptz) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0004 y nuc_0008: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
