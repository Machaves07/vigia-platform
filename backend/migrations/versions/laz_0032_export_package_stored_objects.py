"""Objetos escritos de un paquete exportado (TASK-345; LC-LAZ-22; SUP-U05-23 y 40).

Revisión laz_0032, aditiva: una columna.

``accreditation.export_package.stored_objects`` guarda lo que el flujo de bytes dejó en el prefijo
``export/`` de ``vigia-evidence`` al completar las subidas por partes:
``{"volumes": [{number, size_bytes, sha256, version_id}, …], "document": {size_bytes, sha256,
version_id} | null}``. Las rutas de descarga firman la URL de **esa** versión de cada objeto
(``version_id``) y nunca listan el prefijo. Las claves no se guardan: salen de la organización,
la planta y la exportación (``export.volumes.ExportLocation``).

``NOT NULL DEFAULT '{}'``: la imagen anterior inserta el paquete sin la columna y sigue
funcionando (NFR-NUC-14); un paquete sin objetos escritos no se puede descargar. ``vigia_app`` ya
tiene ``SELECT`` e ``INSERT`` sobre la tabla entera y ningún ``UPDATE`` (el paquete no cambia).
"""

from __future__ import annotations

from alembic import op

revision: str = "laz_0032"
down_revision: str | None = "laz_0031"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE accreditation.export_package
        ADD COLUMN stored_objects jsonb NOT NULL DEFAULT '{}'::jsonb
            CONSTRAINT export_package_stored_objects CHECK (
                jsonb_typeof(stored_objects) = 'object'
                AND (NOT stored_objects ? 'volumes'
                    OR (jsonb_typeof(stored_objects -> 'volumes') = 'array'
                        AND jsonb_array_length(stored_objects -> 'volumes') <= 99))
            )
    """,
    "COMMENT ON COLUMN accreditation.export_package.stored_objects IS 'Volúmenes y documento"
    " escritos bajo export/ con su version_id (TASK-345)'",
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
