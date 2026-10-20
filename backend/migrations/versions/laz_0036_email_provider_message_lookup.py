"""Búsqueda del ``EmailMessage`` por identificador del proveedor (TASK-325; LC-LAZ-34; PR-LAZ-58).

Revisión laz_0036, tras ``laz_0035`` (arrendamiento del paquete mensual). El consumidor
``notifications.adapters.ses.delivery_events`` recibe un aviso de rebote o de queja del servicio
de correo con solo el ``provider_message_id`` (el ``MessageId`` de SES): no conoce la
organización hasta encontrarlo, así que no puede abrir una transacción con su seguridad a nivel
de fila todavía.

Como ``identity.invitation_organization`` (``nuc_0011``) para el enlace de activación,
``notifications.email_provider_message_lookup(provider_message_id text)`` es una función
``SECURITY DEFINER`` de ``vigia_migrate`` con ``search_path`` fijo que **solo devuelve**
``organization_id``, ``message_id`` y ``queued_at`` de la clave con ese identificador (o ninguna
fila): el consumidor abre después su propia transacción con el contexto de esa organización y
actualiza ``email_message`` y los demás bajo su seguridad a nivel de fila de siempre, como
cualquier otra escritura de U-04. La política ``email_provider_message_lookup`` deja a
``vigia_migrate`` leer ``email_provider_message_key`` solo mientras
``vigia.email_provider_message_lookup`` vale ``on``, cosa que solo ocurre dentro de la función;
``vigia_app`` solo puede ejecutarla.
"""

from __future__ import annotations

from alembic import op

revision: str = "laz_0036"
down_revision: str | None = "laz_0035"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    CREATE POLICY email_provider_message_lookup ON notifications.email_provider_message_key
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING (pg_catalog.current_setting('vigia.email_provider_message_lookup', true) = 'on')
    """,
    # Como en nuc_0011: la variable se fija con set_config dentro y se vacía antes de salir.
    """
    CREATE FUNCTION notifications.email_provider_message_lookup(provider_message_id text)
        RETURNS TABLE(organization_id uuid, message_id uuid, queued_at timestamptz)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        PERFORM pg_catalog.set_config('vigia.email_provider_message_lookup', 'on', true);
        RETURN QUERY
            SELECT key.organization_id, key.message_id, key.queued_at
            FROM notifications.email_provider_message_key AS key
            WHERE key.provider_message_id = email_provider_message_lookup.provider_message_id;
        PERFORM pg_catalog.set_config('vigia.email_provider_message_lookup', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION notifications.email_provider_message_lookup(text) IS
        'Organización, mensaje y marca de cola de la clave con ese identificador del proveedor'
    """,
    """
    COMMENT ON POLICY email_provider_message_lookup ON notifications.email_provider_message_key IS
        'Solo dentro de notifications.email_provider_message_lookup
         (vigia.email_provider_message_lookup = on)'
    """,
    "REVOKE ALL ON FUNCTION notifications.email_provider_message_lookup(text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION notifications.email_provider_message_lookup(text) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0011: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
