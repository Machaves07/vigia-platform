"""``deep_link_ref`` de un aviso de toda la organización (TASK-324; LC-LAZ-30 y 31; BR-LAZ-128).

Revisión laz_0034, tras ``laz_0033`` (uso del asistente). ``laz_0030`` cerró
``notification.deep_link_ref`` a los seis tipos de ``domain-entities.md`` §6.2 (``finding``,
``queue_item``, ``export``, ``package``, ``zone``, ``node``), pero el catálogo de §7.2 tiene
eventos que no nombran ninguno de esos objetos: los de usuarios y roles, concesiones, alertas de la
plataforma, integridad, vocabulario y la divergencia de la proyección. Con la columna obligatoria,
esos avisos no podrían existir, y ninguna notificación se puede perder (BR-LAZ-130).

Se **añade** un séptimo tipo, ``organization``, con el identificador de la organización a la que se
refiere el aviso: lleva a la portada de esa organización en la aplicación, que exige sesión y
alcance como cualquier otro enlace (nunca un enlace público). Solo se amplía la lista: los seis
tipos anteriores, la forma ``{kind, id}`` sin otras claves y el UUID siguen iguales, y ningún aviso
escrito deja de cumplir la restricción. La imagen anterior no escribe avisos, así que arranca
igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from alembic import op

revision: str = "laz_0034"
down_revision: str | None = "laz_0033"
branch_labels: None = None
depends_on: None = None

_DEEP_LINK_KIND = "'finding', 'queue_item', 'export', 'package', 'zone', 'node', 'organization'"
_UUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute(
        f"""
        ALTER TABLE notifications.notification
            DROP CONSTRAINT notification_deep_link_ref,
            ADD CONSTRAINT notification_deep_link_ref CHECK (
                jsonb_typeof(deep_link_ref) = 'object'
                AND deep_link_ref - 'kind' - 'id' = '{{}}'::jsonb
                AND jsonb_typeof(deep_link_ref -> 'kind') = 'string'
                AND deep_link_ref ->> 'kind' IN ({_DEEP_LINK_KIND})
                AND jsonb_typeof(deep_link_ref -> 'id') = 'string'
                AND deep_link_ref ->> 'id' ~ {_UUID}
            )
        """
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
