"""``process_context`` en ``catalog.zone_catalog_version.changed_fields`` (TASK-594; A-129 Q7).

Revisión gob_0047, aditiva y solo hacia adelante (NFR-NUC-14).
``PUT /zones/{zone_id}/process-context`` publica una versión del catálogo con
``changed_fields = [process_context]`` (versión 4 de ``catalog_version_published``). La
restricción ``zone_catalog_version_changed_fields`` de ``gob_0017`` solo admitía los ocho valores
de la versión 1: sin ampliarla, cada publicación de las variables de proceso caería con
``CheckViolation``.

La restricción se recrea en una sola sentencia con los nueve valores y las mismas demás reglas (de
1 a 8 valores, sin repetir ni nulos). Solo amplía: toda fila existente la cumple (se valida al
añadirla) y la imagen anterior, que nunca escribe ``process_context``, sigue escribiendo igual.
``MINIMUM_SCHEMA_VERSION`` sube a 47: la publicación de esta imagen escribe el valor nuevo.
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0047"
down_revision: str | None = "gob_0046"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE catalog.zone_catalog_version
        DROP CONSTRAINT zone_catalog_version_changed_fields,
        ADD CONSTRAINT zone_catalog_version_changed_fields CHECK (
            cardinality(changed_fields) BETWEEN 1 AND 8
            AND changed_fields <@ ARRAY['standards', 'cameras', 'minimum_coverage', 'signals',
                'thresholds', 'clip_window', 'episode', 'single_occupancy',
                'process_context']::text[]
            AND catalog.text_array_is_set(changed_fields)
        )
    """,
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
