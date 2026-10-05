"""Número de la lista de revocación global de ``vigia-node-ca`` (TASK-220; D-7, LC-GOB-11).

Revisión gob_0025. ``regenerate_revocation_list`` firma cada ``ca/crl.pem`` con un ``CRLNumber``
creciente (RFC 5280 §5.2.3). ``fleet.revocation_list_state`` (gob_0021) no tenía dónde guardarlo,
y la columna ``crl_number`` de ``revocation_list_publication`` (gob_0018) solo la ve el contexto de
operador (``operator_only``), que el barrido del worker no tiene (A-51: ningún contexto nuevo).

``crl_number`` es el último número **reservado**: el ciclo lo sube en una transacción corta antes de
firmar, así que un número nunca se repite aunque la publicación falle después de escribir el
objeto. Fila global sin datos de cliente, como el resto de ``revocation_list_state``.

``vigia_app`` recibe ``UPDATE`` solo de esta columna.

``fleet.vigia_revocation_list_organizations()``: la lista es de **todas** las organizaciones (D-7),
también las ``suspended``. ``shared.vigia_active_organizations()`` (nuc_0013) solo devuelve las
``active``, y con ella el certificado revocado de un nodo de una organización suspendida volvería a
pasar el balanceador. Mismo patrón que aquella: ``SECURITY DEFINER`` de ``vigia_migrate`` con
``search_path`` fijo, solo con actor ``system``, **solo identificadores** en orden, y la política
``periodic_iteration`` de ``identity.organization`` abierta solo mientras dura la función. Cada
organización se lee después en su propia transacción con ``context_for_organization``.

No cambia ninguna tabla, función ni política existente: la imagen anterior no lee nada de esto y
arranca igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0025"
down_revision: str | None = "gob_0024"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE fleet.revocation_list_state
        ADD COLUMN crl_number bigint NOT NULL DEFAULT 0
            CONSTRAINT revocation_list_state_crl_number CHECK (crl_number >= 0)
    """,
    """
    COMMENT ON COLUMN fleet.revocation_list_state.crl_number IS
        'Último CRLNumber reservado para ca/crl.pem (creciente, RFC 5280; TASK-220)'
    """,
    "GRANT UPDATE (crl_number) ON fleet.revocation_list_state TO vigia_app",
    """
    CREATE FUNCTION fleet.vigia_revocation_list_organizations()
        RETURNS TABLE (organization_id uuid)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    #variable_conflict use_column
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.periodic_iteration', 'on', true);
        RETURN QUERY
            SELECT organization.organization_id
            FROM identity.organization AS organization
            ORDER BY organization.organization_id;
        PERFORM pg_catalog.set_config('vigia.periodic_iteration', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION fleet.vigia_revocation_list_organizations() IS
        'Todas las organizaciones, sea cual sea su estado: solo identificadores y solo con actor'
        ' system (lista de revocación global, TASK-220)'
    """,
    "REVOKE ALL ON FUNCTION fleet.vigia_revocation_list_organizations() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION fleet.vigia_revocation_list_organizations() TO vigia_app",
)


def upgrade() -> None:
    # Como en gob_0021: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
