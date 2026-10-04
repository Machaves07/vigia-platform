"""Reemisión de la concesión de clip y ``first_served_at`` del clip de verificación (TASK-222).

Revisión gob_0021, aditiva sobre las tablas de ``gob_0018`` (LC-GOB-13; PR-GOB-20; nº 32).

- **Reemisión** («vencida y sin objeto → se reemite», decisión del redactor de TASK-222): la
  clave de ``fleet.clip_upload_grant`` es el ``clip_id`` del nodo, así que pedir de nuevo un clip
  cuya concesión venció sin objeto renueva **la misma fila**: ``issued_at`` y ``expires_at``
  entran en la lista blanca de ``catalog.guard_update`` (cuarto argumento, sin tocar cierres ni
  transiciones) y ``vigia_app`` recibe ``UPDATE`` sobre esas dos columnas. La guarda nueva
  ``fleet.clip_grant_reissue_guard`` solo deja moverlas en una concesión ``issued`` que sigue
  ``issued``, **hacia adelante** y **después** de vencer (``OLD.expires_at <= NEW.issued_at``):
  una concesión vigente, usada, vencida o huérfana nunca se renueva. La restricción
  ``clip_upload_grant_expiry`` (≤ 15 minutos) sigue igual.
- **``first_served_at``** (marca final del tramo 3b de NFR-GOB-70, TASK-216): instante en que la
  consola obtuvo por primera vez el clip de verificación (``GET /zones/{zone_id}/commissioning-
  clips``). Es un **cierre** de la tabla ⛓: de nulo a valor una sola vez (``catalog.guard_update``
  con ``{blur_check_result,first_served_at}``), nunca antes de ``received_at``.

Los disparadores se sustituyen con ``CREATE OR REPLACE TRIGGER`` (sin quitar nunca la guarda de
la tabla) y se vuelven a dejar ``ENABLE ALWAYS``. La imagen anterior no lee ni escribe estas
tablas: arranca igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0021"
down_revision: str | None = "gob_0020"
branch_labels: None = None
depends_on: None = None

APP_UPDATABLE_ADDITIONS: dict[str, tuple[str, ...]] = {
    "clip_upload_grant": ("issued_at", "expires_at"),
    "verification_clip": ("first_served_at",),
}
"""Columnas con ``UPDATE`` de ``vigia_app`` que esta revisión añade a las de ``gob_0018``."""

CLIP_GRANT_TRIGGERS = ("state_transition", "reissue_guard")
"""Disparadores de ``fleet.clip_upload_grant`` tras esta revisión."""

_STATEMENTS = (
    # --- Reemisión de la concesión ---------------------------------------------------------------
    """
    CREATE FUNCTION fleet.clip_grant_reissue_guard() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF NEW.issued_at IS DISTINCT FROM OLD.issued_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        THEN
            IF OLD.status <> 'issued' OR NEW.status <> 'issued'
               OR NEW.issued_at <= OLD.issued_at
               OR NEW.issued_at < OLD.expires_at
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'restrict_violation',
                    MESSAGE = 'una concesión de clip solo se reemite emitida y ya vencida (P4)';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE OR REPLACE TRIGGER state_transition BEFORE UPDATE ON fleet.clip_upload_grant
        FOR EACH ROW EXECUTE FUNCTION catalog.guard_update(
            '{used_at,orphaned_at}', 'status', '{issued>used,issued>expired,used>orphan}',
            '{issued_at,expires_at}')
    """,
    "ALTER TABLE fleet.clip_upload_grant ENABLE ALWAYS TRIGGER state_transition",
    """
    CREATE TRIGGER reissue_guard BEFORE UPDATE OF issued_at, expires_at
        ON fleet.clip_upload_grant
        FOR EACH ROW EXECUTE FUNCTION fleet.clip_grant_reissue_guard()
    """,
    "ALTER TABLE fleet.clip_upload_grant ENABLE ALWAYS TRIGGER reissue_guard",
    "GRANT UPDATE (issued_at, expires_at) ON fleet.clip_upload_grant TO vigia_app",
    """
    COMMENT ON FUNCTION fleet.clip_grant_reissue_guard() IS
        'Reemisión de una concesión de clip: solo issued, hacia adelante y tras vencer (TASK-222)'
    """,
    # --- first_served_at -------------------------------------------------------------------------
    "ALTER TABLE fleet.verification_clip ADD COLUMN first_served_at timestamptz",
    """
    ALTER TABLE fleet.verification_clip
        ADD CONSTRAINT verification_clip_first_served_after_received
        CHECK (first_served_at >= received_at)
    """,
    """
    CREATE OR REPLACE TRIGGER append_only_update BEFORE UPDATE ON fleet.verification_clip
        FOR EACH ROW EXECUTE FUNCTION catalog.guard_update(
            '{blur_check_result,first_served_at}', '', '{}', '{}')
    """,
    "ALTER TABLE fleet.verification_clip ENABLE ALWAYS TRIGGER append_only_update",
    "GRANT UPDATE (first_served_at) ON fleet.verification_clip TO vigia_app",
    """
    COMMENT ON COLUMN fleet.verification_clip.first_served_at IS
        'Primera vez que la consola obtuvo el clip (tramo 3b de NFR-GOB-70); cierre único'
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
