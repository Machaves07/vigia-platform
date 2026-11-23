"""Foto del hallazgo en ``fleet.episode_photo``, con régimen por modo (TASK-588; A-128; A-112 §4).

Revisión gob_0045, aditiva y solo hacia adelante (NFR-NUC-14). La misma tabla de la foto de la
sombra (``gob_0042``) guarda también la foto del hallazgo de ``productive``; el modo decide el
régimen:

- ``zone_mode`` (``commissioning`` por omisión, o ``productive``), ``record_id`` (el
  ``finding_id`` o ``detection_id`` que cita la foto; obligatorio en ``productive`` y prohibido en
  ``commissioning``), ``photo_index`` (0 a 20; 0 por omisión) y ``photo_role`` (``peak`` si y solo
  si el índice es 0; ``burst`` del 1 al 20).
- ``expires_at`` pasa a anulable y ``episode_photo_expiry`` se sustituye por
  ``episode_photo_expiry_by_mode``: en ``commissioning``, obligatorio, posterior a la recepción y
  como mucho 90 días después (como hasta ahora); en ``productive``, ``NULL`` (sin vencimiento
  automático, como el clip del hallazgo, BR-NUC-65) o como mucho 1 825 días después.
- Índice único parcial ``(organization_id, zone_id, episode_id, segment_index, photo_index)`` de
  las ``productive`` (una foto por índice y tramo) e índice ``(organization_id, plant_id,
  record_id)`` de las que citan un registro, para la lectura del hallazgo (TASK-589). Lleva
  ``plant_id`` en segunda posición porque todo índice no único de ``fleet`` empieza por el alcance
  ``(organization_id, plant_id)`` (``test_every_index_starts_with_the_scope_or_is_a_key``); la
  lectura del hallazgo conoce su planta.

Las columnas nuevas llevan un valor por omisión constante: PostgreSQL 11+ no reescribe la tabla y
las filas existentes quedan ``commissioning``/``0``/``peak`` con ``record_id`` nulo, sin cambiar
ningún otro valor. La imagen anterior no nombra las columnas nuevas en su ``INSERT``: los valores
por omisión dejan sus filas en ``commissioning`` y sigue arrancando sobre este esquema.
``MINIMUM_SCHEMA_VERSION`` sube a 45: la lectura y la purga de esta imagen filtran por
``zone_mode``.
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0045"
down_revision: str | None = "laz_0044"
branch_labels: None = None
depends_on: None = None

ZONE_MODES: Final = ("commissioning", "productive")
"""Lista cerrada de ``zone_mode`` (A-128)."""
PHOTO_ROLES: Final = ("peak", "burst")
"""Lista cerrada de ``photo_role`` (A-128)."""
MAX_PHOTO_INDEX: Final = 20
"""Índice máximo de la foto en su tramo (ráfaga de la fase 2, A-128 §3)."""
MAX_COMMISSIONING_DAYS: Final = 90
MAX_PRODUCTIVE_DAYS: Final = 1825

_STATEMENTS = (
    f"""
    ALTER TABLE fleet.episode_photo
        ADD COLUMN zone_mode text NOT NULL DEFAULT 'commissioning'
            CONSTRAINT episode_photo_zone_mode_values
            CHECK (zone_mode IN ({", ".join(f"'{m}'" for m in ZONE_MODES)})),
        ADD COLUMN record_id uuid,
        ADD COLUMN photo_index smallint NOT NULL DEFAULT 0
            CONSTRAINT episode_photo_photo_index_range
            CHECK (photo_index BETWEEN 0 AND {MAX_PHOTO_INDEX}),
        ADD COLUMN photo_role text NOT NULL DEFAULT 'peak'
            CONSTRAINT episode_photo_photo_role_values
            CHECK (photo_role IN ({", ".join(f"'{r}'" for r in PHOTO_ROLES)})),
        ADD CONSTRAINT episode_photo_role_of_index
            CHECK ((photo_index = 0) = (photo_role = 'peak')),
        ADD CONSTRAINT episode_photo_record_by_mode
            CHECK ((zone_mode = 'productive') = (record_id IS NOT NULL))
    """,
    "ALTER TABLE fleet.episode_photo ALTER COLUMN expires_at DROP NOT NULL",
    "ALTER TABLE fleet.episode_photo DROP CONSTRAINT episode_photo_expiry",
    f"""
    ALTER TABLE fleet.episode_photo ADD CONSTRAINT episode_photo_expiry_by_mode CHECK (
        CASE WHEN zone_mode = 'commissioning'
            THEN expires_at IS NOT NULL AND expires_at > received_at
                AND expires_at <= received_at + interval '{MAX_COMMISSIONING_DAYS} days'
            ELSE expires_at IS NULL OR (expires_at > received_at
                AND expires_at <= received_at + interval '{MAX_PRODUCTIVE_DAYS} days')
        END
    )
    """,
    """
    CREATE UNIQUE INDEX episode_photo_productive_slot
        ON fleet.episode_photo (organization_id, zone_id, episode_id, segment_index, photo_index)
        WHERE zone_mode = 'productive'
    """,
    """
    CREATE INDEX episode_photo_record
        ON fleet.episode_photo (organization_id, plant_id, record_id)
        WHERE record_id IS NOT NULL
    """,
    "COMMENT ON COLUMN fleet.episode_photo.zone_mode IS 'Régimen de la foto (A-128):"
    " commissioning (material de puesta en marcha, A-112) o productive (foto del hallazgo)'",
    "COMMENT ON COLUMN fleet.episode_photo.record_id IS 'finding_id o detection_id que cita la"
    " foto del hallazgo; nulo en commissioning'",
    "COMMENT ON COLUMN fleet.episode_photo.expires_at IS 'Vencimiento: en commissioning, de 1 a 90"
    " días; en productive, nulo (sin vencimiento automático, como el clip) o hasta 1825 días'",
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
