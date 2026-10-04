"""Búsqueda del nodo declarado para el alta (TASK-206; A-51; nota U03-H-13).

Revisión gob_0019. El alta (``POST enrollment``) es la única ruta del contrato sin certificado: la
plataforma no sabe de qué organización es la petición hasta leer la CSR, cuyo nombre común es el
``node_id`` del nodo declarado (A-51). Con ``FORCE ROW LEVEL SECURITY`` ningún contexto ve
``identity.node_identity`` de otra organización, así que el contexto del alta
(``ScopeContexts.context_from_node_enrollment``) no se podría construir.

``fleet.vigia_node_enrollment_scope(target_node)`` es esa búsqueda, como las de ``nuc_0013`` y
``nuc_0014``: función ``SECURITY DEFINER`` de ``vigia_migrate`` con ``search_path`` fijo que
devuelve **solo** identificadores, el código y el estado del nodo ``target_node`` (una fila o
ninguna) y solo con actor ``system`` (el contexto de búsqueda del constructor). La política nueva
``node_enrollment_lookup`` deja leer a ``vigia_migrate`` la tabla solo mientras
``vigia.node_enrollment_lookup`` vale ``on``, y eso ocurre únicamente dentro de la función.
``vigia_app`` solo puede ejecutarla. No cambia ninguna política, función ni tabla existente: la
imagen anterior sigue igual sobre este esquema.
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0019"
down_revision: str | None = "gob_0018"
branch_labels: None = None
depends_on: None = None

_LOOKUP_FLAG = "pg_catalog.current_setting('vigia.node_enrollment_lookup', true) = 'on'"

_STATEMENTS = (
    f"""
    CREATE POLICY node_enrollment_lookup ON identity.node_identity
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING ({_LOOKUP_FLAG})
    """,
    """
    COMMENT ON POLICY node_enrollment_lookup ON identity.node_identity IS
        'Solo dentro de fleet.vigia_node_enrollment_scope (vigia.node_enrollment_lookup = on)'
    """,
    """
    CREATE FUNCTION fleet.vigia_node_enrollment_scope(target_node uuid)
        RETURNS TABLE (
            node_id uuid, organization_id uuid, plant_id uuid, code text, status text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    #variable_conflict use_column
    BEGIN
        IF target_node IS NULL
            OR pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system'
        THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.node_enrollment_lookup', 'on', true);
        RETURN QUERY
            SELECT node.node_id, node.organization_id, node.plant_id, node.code, node.status
            FROM identity.node_identity AS node
            WHERE node.node_id = target_node;
        PERFORM pg_catalog.set_config('vigia.node_enrollment_lookup', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION fleet.vigia_node_enrollment_scope(uuid) IS
        'Organización, planta, código y estado del nodo declarado target_node, solo con actor '
        'system (alta del nodo, TASK-206)'
    """,
    "REVOKE ALL ON FUNCTION fleet.vigia_node_enrollment_scope(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION fleet.vigia_node_enrollment_scope(uuid) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0013 y nuc_0014: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
