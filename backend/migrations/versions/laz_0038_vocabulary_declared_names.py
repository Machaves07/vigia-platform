"""Vocabulario declarado en la lista de vocabulario de la organización (TASK-350; LC-LAZ-17;
BR-LAZ-84).

Revisión laz_0038, tras ``laz_0037`` (tablas de apoyo de la reconstrucción de la proyección).
``loop.blocked_vocabulary_list`` gana ``declared_names``: los nombres de equipos, máquinas, normas
y los alias de plantas y zonas que la organización declara **como parte de su lista** y que eximen
al patrón de nombre propio de ``person_attribution`` («Prensa Schuler Kompakt»). Los nombres de
las plantas y zonas de ``identity`` ya eximen sin declararse (``PostgresDeclaredNamesStore``).

Es una lista JSON de ``{name_id, kind, value, added_in_version}``: la proyección vigente de los
registros ``vocabulary_list_changed`` (versión 2 del tipo, que lleva los nombres añadidos y
retirados), igual que ``version`` y ``record_id`` de la misma fila. Aditiva: una fila anterior
queda con la lista vacía, y la imagen anterior (que no nombra la columna) sigue insertando y
leyendo la fila igual. ``vigia_app`` gana ``UPDATE`` solo de esta columna.
"""

from __future__ import annotations

from alembic import op

revision: str = "laz_0038"
down_revision: str | None = "laz_0037"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE loop.blocked_vocabulary_list
        ADD COLUMN declared_names jsonb NOT NULL DEFAULT '[]'::jsonb
            CONSTRAINT blocked_vocabulary_list_declared_names_array
                CHECK (jsonb_typeof(declared_names) = 'array')
    """,
    """
    COMMENT ON COLUMN loop.blocked_vocabulary_list.declared_names IS
        'Vocabulario declarado de la organización (equipos, máquinas, normas y alias de plantas y
         zonas) que exime al patrón de nombre propio (BR-LAZ-84, TASK-350)'
    """,
    "GRANT UPDATE (declared_names) ON loop.blocked_vocabulary_list TO vigia_app",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
