"""Alcance de las asignaciones y búsqueda de la invitación (TASK-126, LC-NUC-05).

Revisión nuc_0011. Dos piezas del esquema que la jerarquía y las invitaciones necesitan:

**Alcance de la asignación.** ``identity.role_assignment.scope_id`` no tiene clave foránea,
porque apunta a una organización, una planta o una zona según ``scope_level``. ``nuc_0004`` ya
exige que el alcance de organización sea la propia organización
(``role_assignment_organization_scope``);
faltaba lo mismo para planta y zona (BR-NUC-12, domain-entities §2.5: "dentro de la misma
organización"; seguimiento de la revisión de TASK-107):

- ``identity.check_role_assignment_scope()``: disparador ``BEFORE INSERT`` que exige que una
  asignación de nivel ``plant`` cite una planta de **su** organización y una de nivel ``zone``,
  una zona de su organización. Corre con los permisos de quien inserta, como
  ``check_role_organization_kind``: bajo la seguridad a nivel de fila, una planta o zona de otra
  organización no es visible y la asignación se rechaza igual que si no existiera.

Ninguna asignación existente cambia: el disparador solo mira las nuevas (solo anexar).

**Búsqueda de la invitación.** Quien acepta una invitación no tiene sesión: solo el token del
enlace. Como ``identity.login_organization`` (``nuc_0007``) para el correo,
``identity.invitation_organization(token_hash)`` es una función ``SECURITY DEFINER`` de
``vigia_migrate`` con ``search_path`` fijo que **solo devuelve el ``organization_id``** de la
invitación con ese hash (o nulo), sin mirar su estado: el servicio decide, ya con la seguridad a
nivel de fila de esa organización, si sigue pendiente y vigente, y responde igual a un token
usado, vencido o inexistente. La política ``invitation_lookup`` deja a ``vigia_migrate`` leer
``invitation`` solo mientras ``vigia.invitation_lookup`` vale ``on``, cosa que solo ocurre dentro
de la función; ``vigia_app`` solo puede ejecutarla.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0011"
down_revision: str | None = "nuc_0010"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    CREATE FUNCTION identity.check_role_assignment_scope() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF NEW.scope_level = 'plant' AND NOT EXISTS (
            SELECT FROM identity.plant
            WHERE organization_id = NEW.organization_id AND plant_id = NEW.scope_id
        ) OR NEW.scope_level = 'zone' AND NOT EXISTS (
            SELECT FROM identity.zone
            WHERE organization_id = NEW.organization_id AND zone_id = NEW.scope_id
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'role_assignment_scope_in_organization',
                MESSAGE = format('el alcance %s de la asignación no es de su organización',
                                 NEW.scope_level);
        END IF;
        RETURN NEW;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION identity.check_role_assignment_scope() FROM PUBLIC",
    """
    CREATE TRIGGER role_assignment_scope BEFORE INSERT ON identity.role_assignment
        FOR EACH ROW EXECUTE FUNCTION identity.check_role_assignment_scope()
    """,
    """
    COMMENT ON FUNCTION identity.check_role_assignment_scope() IS
        'Una asignación de planta o zona cita una planta o zona de su organización (BR-NUC-12)'
    """,
    """
    CREATE POLICY invitation_lookup ON identity.invitation AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING (pg_catalog.current_setting('vigia.invitation_lookup', true) = 'on')
    """,
    # Como en nuc_0007: la variable se fija con set_config dentro y se vacía antes de salir.
    """
    CREATE FUNCTION identity.invitation_organization(token text) RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        found uuid;
    BEGIN
        PERFORM pg_catalog.set_config('vigia.invitation_lookup', 'on', true);
        SELECT invitation.organization_id INTO found
        FROM identity.invitation AS invitation
        WHERE invitation.token_hash = token;
        PERFORM pg_catalog.set_config('vigia.invitation_lookup', '', true);
        RETURN found;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION identity.invitation_organization(text) IS
        'Organización de la invitación con ese hash de token (solo el identificador)'
    """,
    """
    COMMENT ON POLICY invitation_lookup ON identity.invitation IS
        'Solo dentro de identity.invitation_organization (vigia.invitation_lookup = on)'
    """,
    "REVOKE ALL ON FUNCTION identity.invitation_organization(text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION identity.invitation_organization(text) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0004: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
