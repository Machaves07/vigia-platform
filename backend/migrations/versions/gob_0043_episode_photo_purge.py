"""Purga de la foto del episodio de la sombra (TASK-573; A-112; QF5).

Revisión gob_0043, aditiva: un privilegio y dos parámetros de almacenamiento.

- ``GRANT DELETE ON fleet.episode_photo TO vigia_app``: la tarea diaria ``purge_episode_photos``
  borra las fotos vencidas y todas las de una zona cuya compuerta de uso se aprobó. La tabla no es
  de solo anexar (no está en ``migrations/append_only.py``): es material de puesta en marcha, fuera
  del expediente y de la cadena (QF10). ``UPDATE`` sigue sin concederse: una foto aceptada no
  cambia.
- ``autovacuum_vacuum_scale_factor = 0`` y ``autovacuum_vacuum_threshold = 50``, en la tabla y en
  su TOAST: tras la purga, la limpieza automática pasa por las tuplas muertas sin esperar a que
  sean el 20 % de la tabla. PostgreSQL no tiene un ``secure_delete`` como SQLite: un ``DELETE``
  deja la tupla muerta, con sus bytes, hasta que ``VACUUM`` la poda y su espacio se reutiliza; esto
  acorta esa ventana, no la cierra (ver el cuerpo del PR de TASK-573 y ``episode_photo_purge``).

La imagen anterior no borra ni lee estos parámetros: arranca igual sobre este esquema
(NFR-NUC-14). ``MINIMUM_SCHEMA_VERSION`` sube a 43: la purga de esta imagen necesita el privilegio.
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0043"
down_revision: str | None = "gob_0042"
branch_labels: None = None
depends_on: None = None

VACUUM_THRESHOLD: Final = 50
"""Tuplas muertas que disparan la limpieza automática de la tabla y de su TOAST."""

_STATEMENTS = (
    "GRANT DELETE ON fleet.episode_photo TO vigia_app",
    f"""
    ALTER TABLE fleet.episode_photo SET (
        autovacuum_vacuum_scale_factor = 0,
        autovacuum_vacuum_threshold = {VACUUM_THRESHOLD},
        toast.autovacuum_vacuum_scale_factor = 0,
        toast.autovacuum_vacuum_threshold = {VACUUM_THRESHOLD}
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
