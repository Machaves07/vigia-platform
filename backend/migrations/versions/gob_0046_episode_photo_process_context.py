"""Valores de proceso en ``fleet.episode_photo`` (TASK-593; A-129 Q6; BR-GOB-84).

Revisión gob_0046, aditiva y solo hacia adelante (NFR-NUC-14). La foto del episodio guarda el
bloque ``process_context`` que trae la presentación (contrato v1.6.0): el resumen de las variables
de proceso del tramo, ya verificado contra el catálogo de la zona en el paso 7 de la ingesta.

- ``process_context jsonb NULL``: nulo sin bloque (un nodo v1.5.0, o un catálogo sin variables);
  con bloque, siempre un objeto (``episode_photo_process_context_object``). Son datos de máquina,
  no de personas: identificadores, números y listas cerradas, sin texto libre (BR-CTR-13).
- Sin índice: solo se lee con la foto (SCR-06, TASK-595).
- La purga (vencimiento y paso a ``productive``, TASK-573) borra la fila entera: el bloque se va
  con ella.

Añadir una columna anulable sin valor por omisión no reescribe la tabla ni cambia ninguna fila; la
imagen anterior no nombra la columna en su ``INSERT`` y sigue arrancando sobre este esquema.
``MINIMUM_SCHEMA_VERSION`` sube a 46: la ingesta de esta imagen escribe la columna.
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0046"
down_revision: str | None = "gob_0045"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE fleet.episode_photo
        ADD COLUMN process_context jsonb
            CONSTRAINT episode_photo_process_context_object
            CHECK (process_context IS NULL OR jsonb_typeof(process_context) = 'object')
    """,
    "COMMENT ON COLUMN fleet.episode_photo.process_context IS 'Valores de proceso del tramo"
    " (process_context del contrato v1.6.0, A-129), verificados contra el catálogo; nulo sin"
    " bloque'",
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
